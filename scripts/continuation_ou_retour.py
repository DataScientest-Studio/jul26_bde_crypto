"""Apres un fort mouvement, le prix continue-t-il ou revient-il en arriere ?

POURQUOI CETTE MESURE
    Notre modele vend quand le prix vient de monter fort : il a appris un
    retour a la moyenne. Encore faut-il que ce retour existe, et qu'il soit
    assez rapide pour la bougie suivante.

CE QU'ON MESURE
    On isole les 5 % de bougies les plus haussieres (et les plus baissieres),
    puis on regarde le rendement moyen sur les 1, 4, 12, 24 et 48 bougies
    suivantes. On le compare TOUJOURS au rendement moyen du marche sur le meme
    horizon : dans un marche haussier, tout monte, et sans ce point de
    comparaison on croirait a tort avoir trouve un signal.

LECTURE
    rendement apres un choc > rendement moyen  -> continuation (momentum)
    rendement apres un choc < rendement moyen  -> retour a la moyenne

Usage :
    python -m scripts.continuation_ou_retour
    python -m scripts.continuation_ou_retour --intervalles 1h 4h --quantile 0.9
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
from api import donnees

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("continuation")

RESULTATS = config.DOCS / "continuation_ou_retour.json"
HORIZONS = (1, 4, 12, 24, 48)


def analyser(serie: pd.DataFrame, quantile: float) -> dict:
    rendements = serie["close"].pct_change()
    hausses = rendements > rendements.quantile(quantile)
    baisses = rendements < rendements.quantile(1 - quantile)

    lignes = []
    for horizon in HORIZONS:
        suite = serie["close"].shift(-horizon) / serie["close"] - 1
        reference = float(suite.dropna().mean())
        apres_hausse = float(suite[hausses].dropna().mean())
        apres_baisse = float(suite[baisses].dropna().mean())
        lignes.append({
            "horizon_bougies": horizon,
            "marche_en_general_pct": round(reference * 100, 3),
            "apres_forte_hausse_pct": round(apres_hausse * 100, 3),
            "apres_forte_baisse_pct": round(apres_baisse * 100, 3),
            # Positif = le mouvement se prolonge ; negatif = il se retourne.
            "ecart_hausse_pct": round((apres_hausse - reference) * 100, 3),
            "ecart_baisse_pct": round((apres_baisse - reference) * 100, 3),
        })
    return {"bougies": len(serie), "chocs_haussiers": int(hausses.sum()),
            "chocs_baissiers": int(baisses.sum()), "horizons": lignes}


def main():
    parser = argparse.ArgumentParser(description="Continuation ou retour a la moyenne")
    parser.add_argument("--pairs", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    parser.add_argument("--intervalles", nargs="+", default=["15m", "1h", "4h"])
    parser.add_argument("--quantile", type=float, default=0.95)
    args = parser.parse_args()

    resultats = {}
    for paire in args.pairs:
        bougies = donnees.bougies_binance(paire, 1000)
        for interval in args.intervalles:
            serie = (bougies[(bougies["symbol"] == paire) & (bougies["interval"] == interval)]
                     .sort_values("open_time").reset_index(drop=True))
            if len(serie) < 200:
                continue
            analyse = analyser(serie, args.quantile)
            resultats[f"{paire}|{interval}"] = analyse
            court = analyse["horizons"][0]
            log.info("%-14s %4d bougies | apres une forte hausse, la bougie suivante fait "
                     "%+.3f %% contre %+.3f %% en general -> %s", f"{paire}|{interval}",
                     analyse["bougies"], court["apres_forte_hausse_pct"],
                     court["marche_en_general_pct"],
                     "continuation" if court["ecart_hausse_pct"] > 0 else "retour a la moyenne")

    if not resultats:
        log.warning("Pas assez de donnees.")
        return

    log.info("")
    log.info("MOYENNE SUR %d JEUX DE DONNEES : ecart au marche apres un choc", len(resultats))
    log.info("%-10s %18s %18s", "horizon", "apres hausse", "apres baisse")
    moyennes = []
    for index, horizon in enumerate(HORIZONS):
        hausse = float(np.mean([r["horizons"][index]["ecart_hausse_pct"] for r in resultats.values()]))
        baisse = float(np.mean([r["horizons"][index]["ecart_baisse_pct"] for r in resultats.values()]))
        moyennes.append({"horizon_bougies": horizon, "ecart_hausse_pct": round(hausse, 3),
                         "ecart_baisse_pct": round(baisse, 3)})
        log.info("%-10s %17.3f %% %17.3f %%", f"{horizon} bougies", hausse, baisse)

    RESULTATS.write_text(json.dumps({
        "quantile_des_chocs": args.quantile,
        "par_jeu_de_donnees": resultats,
        "moyenne": moyennes,
    }, indent=2), encoding="utf-8")
    log.info("Resultats ecrits : %s", RESULTATS)


if __name__ == "__main__":
    main()
