"""Les ordres limites rendraient-ils le bot rentable ?

DEUX FACONS D'ACHETER
    ordre au marche (taker)   on prend le prix affiche, execution certaine
    ordre limite (maker)      on pose son prix et on attend, frais plus bas
                              mais execution INCERTAINE

TARIFS BINANCE (septembre 2026, niveau de base)
    spot     0,100 % maker ET taker   -> l'ordre limite ne fait rien gagner
    futures  0,020 % maker            -> quatre fois moins cher qu'un taker
             0,050 % taker

    C'est donc sur les futures que la question se pose. Notre bot y est deja
    contraint pour vendre a decouvert.

LE PIEGE : LA NON-EXECUTION
    Un ordre limite n'est execute que si le prix vient le chercher. Et il vient
    surtout le chercher quand le marche part dans l'autre sens : on est servi
    sur les trades qui tournent mal, et pas servi sur ceux qui partaient bien.
    C'est la "selection adverse". Une simulation qui suppose toutes les limites
    executees est donc fausse - elle donne le gain sans le cout.

CE QUE SIMULE CE SCRIPT
    1. sensibilite aux frais : les memes trades, factures a differents tarifs ;
    2. ordres limites realistes : l'entree n'a lieu QUE si la bougie suivante
       revient toucher le prix pose ; sinon le signal est perdu. La sortie au
       take profit est une limite (maker), le stop loss un ordre au marche
       (taker), comme dans la vraie vie.

Usage :
    python -m scripts.ordres_limites
    python -m scripts.ordres_limites --jours 180 --pairs BTCUSDT ETHUSDT
"""
from __future__ import annotations

import argparse
import json
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("limites")

RESULTATS = config.DOCS / "ordres_limites.json"

# Cout d'un aller-retour, en fraction du prix.
TARIFS = {
    "spot au marche (0,100 %)": 0.0020,
    "spot avec BNB (0,075 %)": 0.0015,
    "futures au marche (0,050 %)": 0.0010,
    "futures limite a l'entree (0,020 + 0,050 %)": 0.0007,
    "futures tout en limite (0,020 %)": 0.0004,
}


def sensibilite_aux_frais() -> list[dict]:
    """Les trades deja rejoues, refactures a differents tarifs."""
    with postgres_connection() as conn:
        df = pd.read_sql("""
            SELECT interval, style, rendement_brut_pct, bougie_signal
            FROM positions_virtuelles WHERE statut <> 'ouverte'
        """, conn)
    if df.empty:
        return []

    lignes = []
    for (interval, style), groupe in df.groupby(["interval", "style"]):
        groupe = groupe.sort_values("bougie_signal")
        ligne = {"interval": interval, "style": style, "trades": len(groupe),
                 "gain_brut_moyen_pct": round(float(groupe["rendement_brut_pct"].mean()), 4)}
        for nom, frais in TARIFS.items():
            nets = groupe["rendement_brut_pct"] / 100 - frais
            capital = 10_000.0
            for net in nets:
                capital *= 1 + 0.05 * net        # 5 % du capital par position
            ligne[nom] = {"gain_moyen_net_pct": round(float(nets.mean() * 100), 4),
                          "capital_simule": round(capital, 0)}
        lignes.append(ligne)
    return lignes


def simuler_limites(serie: pd.DataFrame, signaux: list[dict], volatilite: np.ndarray,
                    largeur: float, horizon: int, decalage: float) -> dict:
    """Entree par ordre limite : execution seulement si le prix revient.

    `decalage` place la limite un peu MIEUX que le prix courant (en fraction du
    prix) : acheter moins cher, vendre plus cher. Plus il est grand, meilleur
    est le prix obtenu, mais moins souvent on est servi.
    """
    hauts, bas, clotures = (serie["high"].to_numpy(), serie["low"].to_numpy(),
                            serie["close"].to_numpy())
    executes, perdus, resultats = 0, 0, []

    for signal in signaux:
        i, sens = signal["indice"], signal["sens"]
        if i + horizon + 1 >= len(serie) or not np.isfinite(volatilite[i]) or volatilite[i] <= 0:
            continue
        reference = clotures[i]
        limite = reference * (1 - decalage) if sens == 1 else reference * (1 + decalage)

        # Servi seulement si la bougie suivante vient chercher ce prix.
        servi = bas[i + 1] <= limite if sens == 1 else hauts[i + 1] >= limite
        if not servi:
            perdus += 1
            continue
        executes += 1

        distance = largeur * volatilite[i]
        take_profit = limite * (1 + distance) if sens == 1 else limite * (1 - distance)
        stop_loss = limite * (1 - distance) if sens == 1 else limite * (1 + distance)

        issue, prix_sortie = "echeance", clotures[min(i + 1 + horizon, len(serie) - 1)]
        for j in range(i + 2, min(i + 2 + horizon, len(serie))):
            touche_stop = bas[j] <= stop_loss if sens == 1 else hauts[j] >= stop_loss
            touche_gain = hauts[j] >= take_profit if sens == 1 else bas[j] <= take_profit
            if touche_stop:
                issue, prix_sortie = "stop loss", stop_loss
                break
            if touche_gain:
                issue, prix_sortie = "take profit", take_profit
                break

        brut = (prix_sortie / limite - 1) * sens
        # Entree en limite (maker), sortie en limite si take profit, au marche
        # sinon : c'est ainsi qu'un vrai bot fonctionne.
        frais = 0.0002 + (0.0002 if issue == "take profit" else 0.0005)
        resultats.append({"brut": brut, "net": brut - frais, "issue": issue})

    if not resultats:
        return {}
    nets = np.array([r["net"] for r in resultats])
    return {
        "decalage_pct": round(decalage * 100, 3),
        "signaux": executes + perdus,
        "executes": executes,
        "taux_execution_pct": round(100 * executes / max(executes + perdus, 1), 1),
        "gagnants_pct": round(float((nets > 0).mean() * 100), 1),
        "gain_moyen_net_pct": round(float(nets.mean() * 100), 4),
        "issues": {issue: sum(1 for r in resultats if r["issue"] == issue)
                   for issue in ("take profit", "stop loss", "echeance")},
    }


def main():
    parser = argparse.ArgumentParser(description="Ordres limites et frais")
    parser.add_argument("--pairs", nargs="+", default=config.PAIRS)
    parser.add_argument("--intervalles", nargs="+", default=["15m", "1h", "4h"])
    parser.add_argument("--style", default="conservateur")
    parser.add_argument("--jours", type=int, default=365)
    args = parser.parse_args()

    log.info("=== 1. LES MEMES TRADES, FACTURES DIFFEREMMENT ===")
    sensibilite = sensibilite_aux_frais()
    if sensibilite:
        log.info("%-6s %-13s %7s %12s %s", "pas", "style", "trades", "gain brut",
                 " | ".join(f"{nom.split(' (')[0]:>22}" for nom in TARIFS))
        for ligne in sorted(sensibilite, key=lambda l: (l["interval"], l["style"])):
            valeurs = " | ".join(f"{ligne[nom]['gain_moyen_net_pct']:>21.4f} %" for nom in TARIFS)
            log.info("%-6s %-13s %7d %10.4f %% %s", ligne["interval"], ligne["style"],
                     ligne["trades"], ligne["gain_brut_moyen_pct"], valeurs)
    else:
        log.warning("Carnet vide : lancer d'abord scripts.rejouer_positions")

    log.info("")
    log.info("=== 2. ORDRES LIMITES REELS (execution incertaine) ===")
    barrieres = charger_barrieres()
    largeur, horizon = float(barrieres["largeur_barrieres"]), int(barrieres["horizon"])

    limites = {}
    for paire in args.pairs:
        bougies = donnees.bougies_binance(paire, jours=args.jours)
        for interval in args.intervalles:
            serie = (bougies[(bougies["symbol"] == paire) & (bougies["interval"] == interval)]
                     .sort_values("open_time").reset_index(drop=True))
            decisions = modele.predire_serie(bougies, paire, interval, args.style,
                                             limite=len(serie))
            position = {t.isoformat(): i for i, t in enumerate(serie["open_time"])}
            signaux = [{"indice": position[d["open_time"]],
                        "sens": 1 if d["decision"] == "acheter" else -1}
                       for d in decisions if d["decision"] != "attendre" and d["open_time"] in position]
            if len(signaux) < 20:
                continue
            volatilite = volatilite_glissante(serie["close"], FENETRE_VOLATILITE).to_numpy()
            for decalage in (0.0, 0.0005, 0.001):
                mesure = simuler_limites(serie, signaux, volatilite, largeur, horizon, decalage)
                if mesure:
                    limites.setdefault(f"{paire}|{interval}", []).append(mesure)

    if limites:
        log.info("%-6s %-10s %9s %11s %10s %12s", "pas", "decalage", "executes",
                 "taux exec.", "gagnants", "gain net")
        for index, decalage in enumerate((0.0, 0.0005, 0.001)):
            for interval in args.intervalles:
                lignes = [v[index] for cle, v in limites.items()
                          if cle.endswith(f"|{interval}") and len(v) > index]
                if not lignes:
                    continue
                log.info("%-6s %9.3f %% %9d %10.1f %% %9.1f %% %+11.4f %%", interval,
                         decalage * 100, sum(l["executes"] for l in lignes),
                         float(np.mean([l["taux_execution_pct"] for l in lignes])),
                         float(np.mean([l["gagnants_pct"] for l in lignes])),
                         float(np.mean([l["gain_moyen_net_pct"] for l in lignes])))

    RESULTATS.write_text(json.dumps({
        "tarifs_aller_retour": TARIFS,
        "sensibilite_aux_frais": sensibilite,
        "ordres_limites": limites,
    }, indent=2), encoding="utf-8")
    log.info("Resultats ecrits : %s", RESULTATS)


if __name__ == "__main__":
    main()
