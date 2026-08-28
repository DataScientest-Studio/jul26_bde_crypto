"""Collecte temps reel via WebSocket, ecriture en JSON Lines.

Usage :
    python -m scripts.collect_stream --duree 300
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.binance_ws import CollecteurWebSocket

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("stream")


def main():
    p = argparse.ArgumentParser(description="Collecte temps reel Binance")
    p.add_argument("--paires", nargs="+", default=config.PAIRES)
    p.add_argument("--flux", default="kline_1m")
    p.add_argument("--duree", type=float, default=120, help="secondes")
    args = p.parse_args()

    config.DATA_RAW.mkdir(parents=True, exist_ok=True)
    sortie = config.DATA_RAW / f"stream_{args.flux}.jsonl"

    # JSON Lines : une bougie par ligne, ajout en fin de fichier. Contrairement
    # a un tableau JSON, le fichier reste valide meme si la collecte est
    # interrompue brutalement - propriete essentielle pour un flux continu.
    fichier = sortie.open("a", encoding="utf-8")

    def enregistrer(bougie: dict):
        bougie = {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in bougie.items()}
        fichier.write(json.dumps(bougie) + "\n")
        fichier.flush()
        log.info("Bougie cloturee %s %s close=%s", bougie["symbol"], bougie["open_time"], bougie["close"])

    collecteur = CollecteurWebSocket(args.paires, args.flux, sur_bougie=enregistrer)
    try:
        asyncio.run(collecteur.ecouter(duree_max=args.duree))
    except KeyboardInterrupt:
        log.info("Arret demande.")
    finally:
        fichier.close()
        s = collecteur.stats
        taux = 100 * s["ignores"] / s["recus"] if s["recus"] else 0
        log.info("Messages recus=%d | cloturees=%d | ignores=%d (%.1f%%) | reconnexions=%d",
                 s["recus"], s["cloturees"], s["ignores"], taux, s["reconnexions"])
        log.info("Sortie : %s", sortie)


if __name__ == "__main__":
    main()
