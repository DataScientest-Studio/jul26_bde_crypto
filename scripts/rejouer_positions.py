"""Rejoue le bot sur l'historique et remplit le carnet de positions.

POURQUOI
    Le carnet se remplit en temps reel, au rythme des signaux : une position
    toutes les quelques heures. Pour juger le modele - et pour qu'une
    demonstration montre autre chose qu'un tableau vide - on rejoue le passe
    recent comme si le bot avait tourne.

COMMENT, ET CE QUI GARANTIT L'HONNETETE
    - le modele ne voit que des bougies CLOTUREES, dans l'ordre ;
    - une seule position a la fois par paire, pas de temps et style : tant
      qu'une position court, les nouveaux signaux sont ignores. C'est la regle
      du backtest de l'etape 3, et elle evite de gonfler les resultats avec des
      trades qui se recouvrent ;
    - les barrieres sont celles connues A L'OUVERTURE (volatilite des 24
      bougies precedentes), jamais recalculees apres coup ;
    - si le take profit et le stop loss tombent dans la meme bougie, on retient
      le stop loss : les donnees ne disent pas dans quel ordre les prix ont ete
      atteints, on prend l'hypothese defavorable ;
    - frais de 0,1 % a l'entree et 0,1 % a la sortie.

Usage :
    python -m scripts.rejouer_positions
    python -m scripts.rejouer_positions --pairs BTCUSDT --intervalles 15m --jours 45
    python -m scripts.rejouer_positions --vider    # repart d'un carnet vide
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src import config
from src.database import postgres_connection
from src.labeling import volatilite_glissante
from src.preprocessing import INTERVAL_SECONDS
from api import donnees, modele
from api.ordre import FENETRE_VOLATILITE, charger_barrieres
from api.positions import FRAIS_ALLER_RETOUR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("rejeu")


def rejouer(bougies: pd.DataFrame, symbole: str, interval: str, style: str,
            largeur: float, horizon: int) -> list[dict]:
    """Deroule le temps et retourne les positions qu'aurait prises le bot."""
    serie = (bougies[(bougies["symbol"] == symbole) & (bougies["interval"] == interval)]
             .sort_values("open_time").reset_index(drop=True))
    decisions = modele.predire_serie(bougies, symbole, interval, style, limite=len(serie))
    par_bougie = {d["open_time"]: d for d in decisions}
    volatilite = volatilite_glissante(serie["close"], FENETRE_VOLATILITE).to_numpy()

    positions, i = [], 0
    while i < len(serie):
        bougie = serie.iloc[i]
        decision = par_bougie.get(bougie["open_time"].isoformat())
        if decision is None or decision["decision"] == "attendre" or not np.isfinite(volatilite[i]):
            i += 1
            continue

        sens = 1 if decision["decision"] == "acheter" else -1
        entree = float(bougie["close"])
        distance = largeur * float(volatilite[i])
        take_profit = entree * (1 + distance) if sens == 1 else entree * (1 - distance)
        stop_loss = entree * (1 - distance) if sens == 1 else entree * (1 + distance)

        # On avance bougie par bougie jusqu'a ce qu'une barriere tombe.
        statut, prix_sortie, ferme_a, j = None, None, None, i + 1
        while j < len(serie) and j <= i + horizon:
            suivante = serie.iloc[j]
            touche_stop = (suivante["low"] <= stop_loss if sens == 1
                           else suivante["high"] >= stop_loss)
            touche_gain = (suivante["high"] >= take_profit if sens == 1
                           else suivante["low"] <= take_profit)
            if touche_stop:
                statut, prix_sortie, ferme_a = "stop loss", stop_loss, suivante["close_time"]
                break
            if touche_gain:
                statut, prix_sortie, ferme_a = "take profit", take_profit, suivante["close_time"]
                break
            j += 1

        if statut is None:
            if j >= len(serie):
                break                      # position encore ouverte : on s'arrete la
            suivante = serie.iloc[min(j, len(serie) - 1)]
            statut, prix_sortie, ferme_a = "echeance", float(suivante["close"]), suivante["close_time"]

        brut = (prix_sortie / entree - 1) * sens
        positions.append({
            "symbol": symbole, "interval": interval, "style": style, "sens": sens,
            "bougie_signal": bougie["open_time"], "prix_entree": entree,
            "take_profit": take_profit, "stop_loss": stop_loss,
            "echeance": bougie["open_time"] + pd.Timedelta(seconds=INTERVAL_SECONDS[interval]) * (horizon + 1),
            "probabilite_hausse": decision["probabilite_hausse"],
            "statut": statut, "prix_sortie": float(prix_sortie), "fermee_a": ferme_a,
            "rendement_brut_pct": round(brut * 100, 4),
            "rendement_net_pct": round((brut - FRAIS_ALLER_RETOUR) * 100, 4),
        })
        i = j + 1                          # une seule position a la fois

    return positions


def enregistrer(positions: list[dict]) -> int:
    from psycopg2.extras import execute_values

    if not positions:
        return 0
    colonnes = ["symbol", "interval", "style", "sens", "bougie_signal", "ouverte_a",
                "prix_entree", "take_profit", "stop_loss", "echeance", "probabilite_hausse",
                "statut", "prix_sortie", "fermee_a", "rendement_brut_pct", "rendement_net_pct"]
    # `ouverte_a` reprend la date de la bougie : la position est censee avoir
    # ete ouverte a ce moment-la, pas au moment du rejeu.
    lignes = [tuple(p["bougie_signal"] if c == "ouverte_a" else p[c] for c in colonnes)
              for p in positions]

    with postgres_connection() as conn, conn.cursor() as cur:
        execute_values(cur, f"""
            INSERT INTO positions_virtuelles ({", ".join(colonnes)})
            VALUES %s
            ON CONFLICT (symbol, interval, style, bougie_signal) DO NOTHING
        """, lignes)
        cur.execute("SELECT count(*) FROM positions_virtuelles")
        return cur.fetchone()[0]


def main():
    parser = argparse.ArgumentParser(description="Rejeu du bot sur l'historique")
    parser.add_argument("--pairs", nargs="+", default=config.PAIRS)
    parser.add_argument("--intervalles", nargs="+", default=["15m", "1h", "4h"])
    parser.add_argument("--styles", nargs="+", default=["conservateur", "agressif"])
    parser.add_argument("--jours", type=int, default=30)
    parser.add_argument("--vider", action="store_true", help="repartir d'un carnet vide")
    args = parser.parse_args()

    if args.vider:
        with postgres_connection() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE positions_virtuelles")
        log.info("Carnet vide.")

    barrieres = charger_barrieres()
    largeur, horizon = float(barrieres["largeur_barrieres"]), int(barrieres["horizon"])
    log.info("Barrieres : %.0f sigma, echeance %d bougies", largeur, horizon)

    total = 0
    for paire in args.pairs:
        bougies = donnees.bougies_binance(
            paire, min(int(args.jours * 86400 / INTERVAL_SECONDS["15m"]), 1000))
        for interval in args.intervalles:
            for style in args.styles:
                positions = rejouer(bougies, paire, interval, style, largeur, horizon)
                if not positions:
                    continue
                gagnantes = sum(1 for p in positions if p["rendement_net_pct"] > 0)
                moyenne = np.mean([p["rendement_net_pct"] for p in positions])
                total = enregistrer(positions)
                log.info("%-8s %-3s %-12s : %3d positions | %3d gagnantes (%4.1f %%) | "
                         "moyenne %+.3f %%", paire, interval, style, len(positions),
                         gagnantes, 100 * gagnantes / len(positions), moyenne)

    log.info("Carnet : %d positions au total", total)


if __name__ == "__main__":
    main()
