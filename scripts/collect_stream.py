"""Collecte temps reel via WebSocket, ecriture en JSON Lines.

Usage :
    python -m scripts.collect_stream --duration 300
    python -m scripts.collect_stream --pairs BTCUSDT ETHUSDT --stream kline_5m
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
from src.binance_ws import WebSocketCollector

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("stream")


def main():
    parser = argparse.ArgumentParser(description="Collecte temps reel Binance")
    parser.add_argument("--pairs", nargs="+", default=config.PAIRS)
    parser.add_argument("--stream", default="kline_1m")
    parser.add_argument("--duration", type=float, default=120, help="secondes")
    args = parser.parse_args()

    config.DATA_RAW.mkdir(parents=True, exist_ok=True)
    output = config.DATA_RAW / f"stream_{args.stream}.jsonl"

    # JSON Lines : une bougie par ligne, ajout en fin de fichier. Contrairement
    # a un tableau JSON, le fichier reste valide meme si la collecte est
    # interrompue brutalement - propriete essentielle pour un flux continu.
    handle = output.open("a", encoding="utf-8")

    def write_candle(candle: dict):
        # Les datetime pandas ne sont pas serialisables en JSON : on les
        # repasse en texte ISO au moment de l'ecriture.
        serializable = {
            key: (str(value) if hasattr(value, "isoformat") else value)
            for key, value in candle.items()
        }
        handle.write(json.dumps(serializable) + "\n")
        handle.flush()
        log.info(
            "Bougie cloturee %s %s close=%s",
            candle["symbol"], candle["open_time"], candle["close"],
        )

    collector = WebSocketCollector(args.pairs, args.stream, on_candle=write_candle)
    try:
        asyncio.run(collector.listen(max_duration=args.duration))
    except KeyboardInterrupt:
        log.info("Arret demande.")
    finally:
        handle.close()
        stats = collector.stats
        ratio = 100 * stats["skipped"] / stats["received"] if stats["received"] else 0
        log.info(
            "Messages recus=%d | cloturees=%d | ignores=%d (%.1f%%) | reconnexions=%d",
            stats["received"], stats["closed"], stats["skipped"],
            ratio, stats["reconnections"],
        )
        log.info("Sortie : %s", output)


if __name__ == "__main__":
    main()
