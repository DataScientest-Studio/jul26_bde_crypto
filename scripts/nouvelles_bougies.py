"""Collecte les bougies POSTERIEURES a l'extrait fige, pour un test vierge.

Le test historique (mars -> aout 2026) a servi a trop d'essais : a force de
comparer des variantes dessus, on finit par trouver celle qui lui convient
par hasard (Bailey et Lopez de Prado, "The Probability of Backtest
Overfitting"). Seules des bougies que personne n'a jamais regardees donnent
une mesure defendable.

On telecharge a partir du 1er juillet : les semaines precedant la fin de
l'extrait ne servent qu'a CHAUFFER les indicateurs (moyenne mobile 100,
contexte 4h sur 30 bougies = 5 jours). Seules les bougies ouvertes APRES la
derniere bougie de l'extrait serviront de test.

La bougie en cours n'est pas encore cloturee : elle est ecartee, sinon son
prix de cloture serait un prix intermediaire.

Usage :
    python -m scripts.nouvelles_bougies
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src import config
from src.binance_rest import BinanceClient
from src.preprocessing import deduplicate, normalize_klines

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nouvelles")

SORTIE = config.DATA_PROCESSED / "nouvelles_bougies"
FICHIER = SORTIE / "day_trading_recent.parquet"
INTERVALLES = ("15m", "1h", "4h")
DEBUT_CHAUFFE = "2026-07-01"


def main():
    extrait = pd.read_parquet(config.ROOT / "data" / "extract" / "day_trading.parquet",
                              columns=["open_time"])
    fin_extrait = extrait["open_time"].max()
    maintenant = pd.Timestamp.now(tz="UTC")

    client = BinanceClient()
    morceaux = []
    for interval in INTERVALLES:
        for symbole in config.PAIRS:
            brut = client.fetch_klines_history(symbole, interval, DEBUT_CHAUFFE)
            df = deduplicate(normalize_klines(brut, symbole, interval))
            # Bougie en cours : sa "cloture" n'est qu'un prix intermediaire.
            df = df[df["close_time"] < maintenant]
            morceaux.append(df)

    bougies = pd.concat(morceaux, ignore_index=True)
    SORTIE.mkdir(parents=True, exist_ok=True)
    bougies.to_parquet(FICHIER, index=False)

    nouvelles = bougies[bougies["open_time"] > fin_extrait]
    empreinte = hashlib.sha256(FICHIER.read_bytes()).hexdigest()
    manifeste = {
        "fin_extrait_fige": str(fin_extrait),
        "collecte_le": str(maintenant),
        "debut_chauffe": DEBUT_CHAUFFE,
        "bougies_totales": len(bougies),
        "bougies_de_test": len(nouvelles),
        "periode_de_test": [str(nouvelles["open_time"].min()), str(nouvelles["open_time"].max())],
        "par_intervalle": nouvelles.groupby("interval").size().to_dict(),
        "sha256": empreinte,
    }
    (SORTIE / "manifest.json").write_text(json.dumps(manifeste, indent=2), encoding="utf-8")

    log.info("Fin de l'extrait fige : %s", fin_extrait)
    log.info("Bougies de test (jamais vues) : %d, du %s au %s",
             len(nouvelles), nouvelles["open_time"].min(), nouvelles["open_time"].max())
    log.info("Par intervalle : %s", manifeste["par_intervalle"])
    log.info("Ecrit : %s (sha256 %s...)", FICHIER, empreinte[:12])


if __name__ == "__main__":
    main()
