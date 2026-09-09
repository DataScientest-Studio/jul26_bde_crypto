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
from src.binance_rest import BinanceClient
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


def load_exchange_info_to_mongo(pairs: list[str]) -> int:
    """Recupere les metadonnees des paires et les stocke telles quelles.

    C'est l'exemple qui a motive le choix d'une base document : chaque type
    de filtre a des cles differentes (PRICE_FILTER porte tickSize, LOT_SIZE
    porte stepSize, NOTIONAL autre chose encore). En relationnel il faudrait
    une table par type, ou une table cle-valeur generique.

    On conserve un historique : Binance modifie ses filtres au fil du temps,
    et savoir quand un pas de cotation a change a de la valeur.
    """
    client = BinanceClient()
    info = client.exchange_info(pairs)
    fetched_at = pd.Timestamp.now(tz="UTC").to_pydatetime()

    documents = [
        {**symbol_info, "fetched_at": fetched_at, "endpoint": "/api/v3/exchangeInfo"}
        for symbol_info in info["symbols"]
    ]
    if not documents:
        return 0

    with mongo_client() as mongo:
        mongo[config.MONGO_DB_NAME]["exchange_info"].insert_many(documents, ordered=False)
    return len(documents)


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
    """Charge la reponse brute dans MongoDB, en lots de 1000 bougies.

    MongoDB plafonne un document a 16 Mo. Nos fichiers 1m font 45 Mo : ils ne
    peuvent pas tenir dans un seul document.

    On decoupe donc en lots de 1000 bougies, ce qui n'est pas un compromis
    technique mais la structure REELLE de la donnee : Binance ne renvoie
    jamais plus de 1000 bougies par appel, et notre collecte a justement
    enchaine des lots de cette taille. Un document = une reponse de l'API.

    Retourne la cle metier du groupe, pas un ObjectId : c'est elle qui relie
    les deux bases (voir docs/architecture_etape2.pdf, section 07).
    """
    path = config.DATA_RAW / f"{symbol}_{interval}_raw.json"
    if not path.exists():
        return None

    payload = json.loads(path.read_text(encoding="utf-8"))
    taille_lot = config.KLINES_MAX_PER_REQUEST
    fetched_at = pd.Timestamp.now(tz="UTC").to_pydatetime()

    documents = []
    for index, debut in enumerate(range(0, len(payload), taille_lot)):
        lot = payload[debut:debut + taille_lot]
        documents.append({
            "symbol": symbol,
            "interval": interval,
            "batch_index": index,
            "fetched_at": fetched_at,
            "source": "REST",
            "endpoint": "/api/v3/klines",
            # Bornes du lot, pour retrouver un document sans le parcourir.
            "first_open_time": lot[0][0],
            "last_open_time": lot[-1][0],
            "candle_count": len(lot),
            "payload": lot,
        })

    with mongo_client() as client:
        collection = client[config.MONGO_DB_NAME]["raw_klines"]
        # On remplace le groupe entier : rejouer le pipeline ne doit pas
        # empiler des copies de plusieurs dizaines de Mo.
        collection.delete_many({"symbol": symbol, "interval": interval})
        if documents:
            collection.insert_many(documents, ordered=False)

    return f"{symbol}/{interval}"


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
    # Pas de conversion de date : pd.Timestamp herite de datetime.datetime,
    # psycopg2 l'adapte donc nativement en timestamptz. Passer par
    # .dt.to_pydatetime() etait inutile et declenchait un FutureWarning.

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

        # ATTENTION : apres execute_values, cur.rowcount ne compte que le
        # DERNIER lot envoye (page_size), pas le total. Sur 70 124 lignes il
        # renvoie 124. On interroge donc la table pour connaitre le nombre
        # reellement stocke - c'est de toute facon la seule mesure qui a du
        # sens pour un journal d'audit.
        cur.execute(
            f"SELECT count(*) FROM {table} WHERE symbol = %s AND interval = %s",
            (symbol, interval),
        )
        inserted = cur.fetchone()[0]

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
        "rows_stored": inserted,
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
        if not args.skip_raw:
            try:
                n = load_exchange_info_to_mongo(args.pairs)
                log.info("exchange_info : %d paires recuperees depuis Binance", n)
            except Exception as exc:
                log.warning("exchange_info indisponible (%s) : le referentiel "
                            "sera minimal, sans les precisions de cotation",
                            type(exc).__name__)

        count = sync_symbols(conn, args.pairs)
        log.info("Referentiel : %d paires synchronisees", count)

        echecs = []
        for symbol, interval in plan:
            # Chaque jeu de donnees est valide independamment : un echec sur
            # une paire ne doit pas annuler les 34 autres. Sans ce commit par
            # jeu, une seule exception ramenerait la base a zero.
            try:
                raw_ref = None
                if not args.skip_raw:
                    try:
                        raw_ref = load_raw_to_mongo(symbol, interval)
                    except Exception as exc:
                        # La couche brute est un confort, pas une dependance :
                        # PostgreSQL reste alimentable sans elle.
                        log.warning("%-9s %-4s : MongoDB indisponible (%s), "
                                    "chargement PostgreSQL quand meme",
                                    symbol, interval, type(exc).__name__)

                result = load_candles_to_postgres(conn, symbol, interval, raw_ref)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                echecs.append((symbol, interval, f"{type(exc).__name__}: {exc}"))
                log.error("%-9s %-4s : ECHEC %s", symbol, interval, type(exc).__name__)
                continue

            if result["status"] != "ok":
                log.warning("%-9s %-4s : %s", symbol, interval, result["status"])
                continue

            total_rows += result["rows_stored"]
            log.info("%-9s %-4s -> %-20s lu %7d | en base %7d | %ss",
                     symbol, interval, result["table"],
                     result["rows_read"], result["rows_stored"], result["seconds"])

    log.info("Termine : %d lignes chargees en %.0f s", total_rows, time.time() - started)
    if echecs:
        log.error("%d jeu(x) en echec :", len(echecs))
        for symbol, interval, message in echecs:
            log.error("  %s %s : %s", symbol, interval, message[:120])
        sys.exit(1)


if __name__ == "__main__":
    main()
