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
from api import donnees, modele
from api.ordre import FENETRE_VOLATILITE, charger_barrieres
from api.positions import inserer, simuler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("rejeu")


def rejouer(bougies: pd.DataFrame, symbole: str, interval: str, style: str,
            largeur: float, horizon: int) -> list[dict]:
    """Deroule le temps et retourne les positions qu'aurait prises le bot.

    Le moteur est celui de l'API (api.positions.simuler) : le rejeu historique
    et le rattrapage en direct comptent exactement de la meme facon.
    """
    serie = (bougies[(bougies["symbol"] == symbole) & (bougies["interval"] == interval)]
             .sort_values("open_time").reset_index(drop=True))
    decisions = {d["open_time"]: d for d in
                 modele.predire_serie(bougies, symbole, interval, style, limite=len(serie))}
    volatilite = volatilite_glissante(serie["close"], FENETRE_VOLATILITE).to_numpy()
    return simuler(serie, decisions, volatilite, largeur, horizon, interval, symbole, style)


def enregistrer(positions: list[dict]) -> int:
    with postgres_connection() as conn, conn.cursor() as cur:
        inserer(cur, positions)
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
        bougies = donnees.bougies_binance(paire, jours=args.jours)
        log.info("%s : %d bougies lues", paire, len(bougies))
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
