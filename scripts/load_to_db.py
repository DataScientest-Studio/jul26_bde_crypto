"""Pipeline d'ingestion : des fichiers vers les deux bases.

    data/raw/*.json      -> MongoDB      (couche brute, intacte)
    data/processed/*.parquet -> PostgreSQL (couche exploitable, nettoyee)

Chaque bougie ecrite en PostgreSQL porte une reference vers le document
MongoDB dont elle est issue : depuis n'importe quelle ligne, on remonte a
la reponse exacte de Binance qui l'a produite.

Usage :
    python -m scripts.load_to_db --check          # teste les connexions
    python -m scripts.load_to_db                  # tout charger
    python -m scripts.load_to_db --profile swing  # un profil
    python -m scripts.load_to_db --pairs BTCUSDT --intervals 1h
    python -m scripts.load_to_db --skip-raw       # PostgreSQL seulement
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src import config
from src.database import check_connections, mongo_client, postgres_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("load")

# Colonnes ecrites en base, dans l'ordre attendu par l'INSERT.
CANDLE_COLUMNS = [
    "symbol", "interval", "open_time", "close_time",
    "open", "high", "low", "close",
    "volume", "quote_volume", "nb_trades",
    "taker_buy_base", "taker_buy_quote",
    "source", "raw_ref",
]


def target_table(conn, interval: str) -> str:
    """Table de faits qui stocke ce pas de temps.

    La reponse vient de la base, pas d'un dictionnaire Python : c'est
    `intervals.owner_table` qui fait autorite. Changer la repartition ne
    demande donc pas de retoucher ce script.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT owner_table FROM intervals WHERE interval = %s", (interval,))
        row = cur.fetchone()
    if row is None:
        raise ValueError(
            f"Pas de temps {interval!r} inconnu en base. "
            f"Verifier sql/02_seed.sql."
        )
    return row[0]


def owning_profile(conn, interval: str) -> str | None:
    """Profil proprietaire de ce pas de temps, pour le journal des collectes."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT profile FROM profile_intervals WHERE interval = %s AND is_owner",
            (interval,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def sync_symbols(conn, pairs: list[str]) -> int:
    """Alimente le referentiel des paires depuis MongoDB, sinon en minimal.

    Les tables de faits ont une cle etrangere vers `symbols` : ces lignes
    doivent exister AVANT toute insertion de bougie.
    """
    metadata: dict[str, dict] = {}
    try:
        with mongo_client() as client:
            collection = client[config.MONGO_DB_NAME]["exchange_info"]
            for symbol in pairs:
                doc = collection.find_one({"symbol": symbol}, sort=[("fetched_at", -1)])
                if doc:
                    metadata[symbol] = doc
    except Exception as exc:
        log.warning("exchange_info illisible dans MongoDB (%s), referentiel minimal",
                    type(exc).__name__)

    rows = []
    for symbol in pairs:
        doc = metadata.get(symbol, {})
        filters = {f["filterType"]: f for f in doc.get("filters", [])}
        rows.append((
            symbol,
            doc.get("baseAsset", symbol[:-4] if symbol.endswith("USDT") else symbol),
            doc.get("quoteAsset", "USDT" if symbol.endswith("USDT") else "?"),
            doc.get("status", "TRADING"),
            filters.get("PRICE_FILTER", {}).get("tickSize"),
            filters.get("LOT_SIZE", {}).get("stepSize"),
        ))

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO symbols (symbol, base_asset, quote_asset, status, tick_size, step_size)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol) DO UPDATE SET
                status     = EXCLUDED.status,
                tick_size  = COALESCE(EXCLUDED.tick_size, symbols.tick_size),
                step_size  = COALESCE(EXCLUDED.step_size, symbols.step_size),
                updated_at = now()
            """,
            rows,
        )
    return len(rows)


def load_raw_to_mongo(symbol: str, interval: str) -> str | None:
    """Charge la reponse brute dans MongoDB et retourne son identifiant.

    Un document par (paire, pas de temps) : on remplace au lieu d'empiler,
    sinon relancer le pipeline dupliquerait des dizaines de Mo.
    """
    path = config.DATA_RAW / f"{symbol}_{interval}_raw.json"
    if not path.exists():
        return None

    payload = json.loads(path.read_text(encoding="utf-8"))

    with mongo_client() as client:
        collection = client[config.MONGO_DB_NAME]["raw_klines"]
        result = collection.replace_one(
            {"symbol": symbol, "interval": interval},
            {
                "symbol": symbol,
                "interval": interval,
                "fetched_at": pd.Timestamp.now(tz="UTC").to_pydatetime(),
                "source": "REST",
                "endpoint": "/api/v3/klines",
                "candle_count": len(payload),
                "payload": payload,
            },
            upsert=True,
        )
        if result.upserted_id is not None:
            return str(result.upserted_id)
        doc = collection.find_one({"symbol": symbol, "interval": interval}, {"_id": 1})
        return str(doc["_id"]) if doc else None


def load_candles_to_postgres(conn, symbol: str, interval: str, raw_ref: str | None) -> dict:
    """Charge un fichier Parquet dans la table de faits du bon profil.

    L'insertion est idempotente : relancer le pipeline ne cree pas de
    doublon et met a jour les bougies deja presentes. C'est indispensable
    pour une collecte qui tournera en continu a l'etape 5.
    """
    from psycopg2.extras import execute_values

    path = config.DATA_PROCESSED / f"{symbol}_{interval}.parquet"
    if not path.exists():
        return {"status": "absent", "rows_read": 0, "rows_inserted": 0}

    df = pd.read_parquet(path)
    if df.empty:
        return {"status": "vide", "rows_read": 0, "rows_inserted": 0}

    table = target_table(conn, interval)
    profile = owning_profile(conn, interval)

    df = df.copy()
    df["source"] = "REST"
    df["raw_ref"] = f"raw_klines/{raw_ref}" if raw_ref else None
    # Les Timestamp pandas passent en datetime natifs pour psycopg2.
    for column in ("open_time", "close_time"):
        df[column] = df[column].dt.to_pydatetime()

    records = list(df[CANDLE_COLUMNS].itertuples(index=False, name=None))

    started = time.time()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingestion_runs
                (profile, symbol, interval, target_table, source, raw_collection,
                 rows_read, period_start, period_end, status)
            VALUES (%s, %s, %s, %s, 'REST', 'raw_klines', %s, %s, %s, 'running')
            RETURNING run_id
            """,
            (profile, symbol, interval, table, len(df),
             df["open_time"].min(), df["open_time"].max()),
        )
        run_id = cur.fetchone()[0]

        execute_values(
            cur,
            f"""
            INSERT INTO {table} ({", ".join(CANDLE_COLUMNS)})
            VALUES %s
            ON CONFLICT (symbol, interval, open_time) DO UPDATE SET
                close      = EXCLUDED.close,
                high       = EXCLUDED.high,
                low        = EXCLUDED.low,
                volume     = EXCLUDED.volume,
                nb_trades  = EXCLUDED.nb_trades,
                source     = EXCLUDED.source,
                raw_ref    = EXCLUDED.raw_ref
            """,
            records,
            page_size=5000,
        )
        inserted = cur.rowcount

        cur.execute(
            """
            UPDATE ingestion_runs
               SET finished_at = now(), rows_inserted = %s, status = 'success'
             WHERE run_id = %s
            """,
            (inserted, run_id),
        )

    return {
        "status": "ok",
        "table": table,
        "rows_read": len(df),
        "rows_inserted": inserted,
        "seconds": round(time.time() - started, 1),
    }


def build_plan(args) -> list[tuple[str, str]]:
    """Liste des couples (paire, pas de temps) a charger."""
    if args.intervals:
        intervals = args.intervals
    else:
        profiles = list(config.TRADING_PROFILES) if "all" in args.profile else args.profile
        intervals = sorted(config.resolve_intervals(profiles))
    return [(symbol, interval) for interval in intervals for symbol in args.pairs]


def main():
    parser = argparse.ArgumentParser(description="Ingestion vers PostgreSQL et MongoDB")
    parser.add_argument("--pairs", nargs="+", default=config.PAIRS)
    parser.add_argument("--profile", nargs="+", default=["all"])
    parser.add_argument("--intervals", nargs="+")
    parser.add_argument("--skip-raw", action="store_true",
                        help="Ne pas charger la couche brute dans MongoDB")
    parser.add_argument("--check", action="store_true",
                        help="Tester les connexions et sortir")
    args = parser.parse_args()

    log.info("Verification des connexions...")
    for name, state in check_connections().items():
        log.info("  %-9s %s", name, state)
        if state.startswith("ECHEC"):
            log.error("Base injoignable. Lancer d'abord : docker compose up -d")
            sys.exit(1)
    if args.check:
        return

    plan = build_plan(args)
    log.info("%d jeux de donnees a charger", len(plan))

    total_rows = 0
    started = time.time()

    with postgres_connection() as conn:
        count = sync_symbols(conn, args.pairs)
        log.info("Referentiel : %d paires synchronisees", count)

        for symbol, interval in plan:
            raw_ref = None if args.skip_raw else load_raw_to_mongo(symbol, interval)
            result = load_candles_to_postgres(conn, symbol, interval, raw_ref)

            if result["status"] != "ok":
                log.warning("%-9s %-4s : %s", symbol, interval, result["status"])
                continue

            total_rows += result["rows_inserted"]
            log.info("%-9s %-4s -> %-20s %7d lignes en %ss",
                     symbol, interval, result["table"],
                     result["rows_inserted"], result["seconds"])

    log.info("Termine : %d lignes chargees en %.0f s",
             total_rows, time.time() - started)


if __name__ == "__main__":
    main()
