"""Collecte l'historique complet des paires configurees.

Usage :
    python -m scripts.collect_history
    python -m scripts.collect_history --paires BTCUSDT ETHUSDT --intervalle 4h
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.binance_rest import ClientBinance
from src.preprocessing import controler_qualite, dedupliquer, normaliser_klines

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("collecte")


def main():
    p = argparse.ArgumentParser(description="Collecte d'historique Binance")
    p.add_argument("--paires", nargs="+", default=config.PAIRES)
    p.add_argument("--intervalle", default=config.INTERVALLE)
    p.add_argument("--debut", default=config.DEBUT_HISTORIQUE)
    args = p.parse_args()

    config.DATA_RAW.mkdir(parents=True, exist_ok=True)
    config.DATA_PROCESSED.mkdir(parents=True, exist_ok=True)
    config.SAMPLES.mkdir(parents=True, exist_ok=True)

    client = ClientBinance()
    log.info("Hote %s | %d paires | intervalle %s | depuis %s",
             client.base, len(args.paires), args.intervalle, args.debut)

    # Le rapport qualite FUSIONNE avec les runs precedents : une collecte
    # ponctuelle sur une seule paire ne doit pas effacer le bilan des autres.
    chemin_rapport = config.RACINE / "docs" / "rapport_qualite.json"
    rapport = {"collectes": {}}
    if chemin_rapport.exists():
        rapport = json.loads(chemin_rapport.read_text(encoding="utf-8"))
        rapport.setdefault("collectes", {})

    for symbole in args.paires:
        t0 = time.time()
        brut = client.klines_historique(symbole, args.intervalle, args.debut)

        # On archive le BRUT avant toute transformation : si le pre-processing
        # evolue, on rejoue depuis le disque sans re-solliciter l'API.
        chemin_brut = config.DATA_RAW / f"{symbole}_{args.intervalle}_raw.json"
        chemin_brut.write_text(json.dumps(brut), encoding="utf-8")

        df = dedupliquer(normaliser_klines(brut, symbole, args.intervalle))
        chemin_propre = config.DATA_PROCESSED / f"{symbole}_{args.intervalle}.parquet"
        df.to_parquet(chemin_propre, index=False)

        qualite = controler_qualite(df, args.intervalle)
        qualite["duree_collecte_s"] = round(time.time() - t0, 1)
        qualite["taille_brut_ko"] = round(chemin_brut.stat().st_size / 1024)
        qualite["taille_parquet_ko"] = round(chemin_propre.stat().st_size / 1024)
        qualite["debut_demande"] = args.debut
        # Cle = symbole + intervalle : collecter BTCUSDT en 4h n'ecrase pas le 1h.
        rapport["collectes"][f"{symbole}_{args.intervalle}"] = qualite

        log.info("%s : %d lignes | completude %s%% | %ss | brut %s ko -> parquet %s ko",
                 symbole, qualite["lignes"], qualite["taux_completude_pct"],
                 qualite["duree_collecte_s"], qualite["taille_brut_ko"], qualite["taille_parquet_ko"])

    chemin_rapport.write_text(json.dumps(rapport, indent=2), encoding="utf-8")
    log.info("Rapport qualite ecrit : %s", chemin_rapport)


if __name__ == "__main__":
    main()
