"""Collecte l'historique des paires configurees, par profil de trading.

Usage :
    python -m scripts.collect_history                          # profil par defaut
    python -m scripts.collect_history --profile scalping
    python -m scripts.collect_history --profile swing day_trading
    python -m scripts.collect_history --profile all
    python -m scripts.collect_history --intervals 1h 4h --start 2024-01-01
    python -m scripts.collect_history --list                   # decrit les profils
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
from src.binance_rest import BinanceClient, start_date_for
from src.preprocessing import check_quality, deduplicate, normalize_klines

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("collect")


def describe_profiles():
    """Affiche les profils disponibles et ce qu'ils impliquent."""
    print("\nProfils de trading disponibles\n" + "=" * 70)
    for name, profile in config.TRADING_PROFILES.items():
        depth = profile["history_days"]
        depth_label = "tout l'historique" if depth is None else f"{depth} jours"
        print(f"\n  {name}  ({profile['label']})")
        print(f"    {profile['description']}")
        print(f"    Intervalles  : {', '.join(profile['intervals'])}")
        print(f"    Profondeur   : {depth_label}")
        print(f"    Pourquoi     : {profile['reason']}")
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collecte d'historique Binance")
    parser.add_argument("--pairs", nargs="+", default=config.PAIRS)
    parser.add_argument(
        "--profile",
        nargs="+",
        default=[config.DEFAULT_PROFILE],
        help="scalping, day_trading, swing, ou 'all' pour tout collecter",
    )
    parser.add_argument(
        "--intervals",
        nargs="+",
        help="Intervalles explicites. Ignore les profils si fourni.",
    )
    parser.add_argument(
        "--start",
        help="Date de debut (AAAA-MM-JJ). Ignore la profondeur du profil si fournie.",
    )
    parser.add_argument("--list", action="store_true", help="Decrit les profils et sort")
    return parser


def resolve_plan(args) -> dict[str, str]:
    """Determine quels intervalles collecter, et depuis quelle date chacun.

    Deux modes : soit on part des profils (cas normal), soit l'utilisateur
    impose ses intervalles en ligne de commande (exploration ponctuelle).
    """
    if args.intervals:
        start = args.start or config.HISTORY_START
        return {interval: start for interval in args.intervals}

    profiles = list(config.TRADING_PROFILES) if "all" in args.profile else args.profile
    resolved = config.resolve_intervals(profiles)

    # --start impose la meme date a tous les intervalles, quel que soit le profil.
    return {
        interval: args.start or start_date_for(depth)
        for interval, depth in resolved.items()
    }


def main():
    args = build_parser().parse_args()

    if args.list:
        describe_profiles()
        return

    config.DATA_RAW.mkdir(parents=True, exist_ok=True)
    config.DATA_PROCESSED.mkdir(parents=True, exist_ok=True)

    plan = resolve_plan(args)
    client = BinanceClient()

    log.info("Hote %s | %d paires | %d intervalles", client.base_url, len(args.pairs), len(plan))
    for interval, start in sorted(plan.items()):
        log.info("   %-4s depuis %s", interval, start)

    # Le rapport qualite FUSIONNE avec les runs precedents : une collecte
    # ponctuelle sur une seule paire ne doit pas effacer le bilan des autres.
    report_path = config.DOCS / "rapport_qualite.json"
    report = {"collections": {}}
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report.setdefault("collections", {})

    total_rows = 0
    started = time.time()

    for interval, start in sorted(plan.items()):
        for symbol in args.pairs:
            raw = client.fetch_klines_history(symbol, interval, start)

            # On archive le BRUT avant toute transformation : si le pre-processing
            # evolue, on rejoue depuis le disque sans re-solliciter l'API.
            raw_path = config.DATA_RAW / f"{symbol}_{interval}_raw.json"
            raw_path.write_text(json.dumps(raw), encoding="utf-8")

            df = deduplicate(normalize_klines(raw, symbol, interval))
            clean_path = config.DATA_PROCESSED / f"{symbol}_{interval}.parquet"
            df.to_parquet(clean_path, index=False)

            quality = check_quality(df, interval)
            quality["requested_start"] = start
            quality["raw_size_kb"] = round(raw_path.stat().st_size / 1024)
            quality["parquet_size_kb"] = round(clean_path.stat().st_size / 1024)

            # Cle = symbole + intervalle : collecter BTCUSDT en 4h n'ecrase pas le 1h.
            report["collections"][f"{symbol}_{interval}"] = quality
            total_rows += quality["rows"]

            log.info(
                "%-9s %-4s : %7d lignes | completude %7s%% | %5s ko parquet",
                symbol, interval, quality["rows"],
                quality["completeness_pct"], quality["parquet_size_kb"],
            )

    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log.info(
        "Termine : %d lignes sur %d jeux de donnees en %.0f s. Rapport : %s",
        total_rows, len(plan) * len(args.pairs), time.time() - started, report_path,
    )


if __name__ == "__main__":
    main()
