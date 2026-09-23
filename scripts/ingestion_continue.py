"""Ingestion continue : les bougies cloturees depuis la derniere en base.

C'est la version "en production" de la collecte des etapes 1 et 2. Airflow
l'appelle toutes les 15 minutes ; elle ne telecharge que ce qui manque.

    Binance REST  ->  MongoDB raw_klines          (reponse brute, intacte)
                  ->  PostgreSQL candles_*         (bougies nettoyees)

Les MEMES regles que le chargement initial, et le meme code quand c'est
possible (normalize_klines, upsert_candles, target_table) :
  - le brut est archive dans MongoDB AVANT toute transformation ;
  - chaque bougie PostgreSQL garde une reference (raw_ref) vers le document
    MongoDB dont elle vient ;
  - l'ecriture est idempotente : une tache relancee par Airflow apres un
    echec ne cree aucun doublon, ni dans MongoDB ni dans PostgreSQL.

Seules les bougies CLOTUREES sont ecrites : la bougie en cours change encore,
son prix de cloture ne serait qu'un prix intermediaire.

Usage :
    python -m scripts.ingestion_continue
    python -m scripts.ingestion_continue --pairs BTCUSDT --intervals 15m
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src import config
from src.binance_rest import BinanceClient, start_date_for
from src.database import mongo_client, postgres_connection
from src.preprocessing import deduplicate, interval_to_timedelta, normalize_klines
from scripts.load_to_db import CANDLE_COLUMNS, owning_profile, target_table, upsert_candles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ingestion")

INTERVALLES = config.TRADING_PROFILES["day_trading"]["intervals"]
SOURCE = "REST-continu"


def derniere_bougie(conn, table: str, symbole: str, interval: str) -> pd.Timestamp | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT max(open_time) FROM {table} WHERE symbol = %s AND interval = %s",
                    (symbole, interval))
        valeur = cur.fetchone()[0]
    return pd.Timestamp(valeur) if valeur is not None else None


def debut_de_collecte(derniere: pd.Timestamp | None, interval: str) -> pd.Timestamp:
    """La bougie qui SUIT la derniere en base ; tout l'historique du profil sinon."""
    if derniere is None:
        return pd.Timestamp(start_date_for(config.TRADING_PROFILES["day_trading"]["history_days"]),
                            tz="UTC")
    return derniere + interval_to_timedelta(interval)


def garder_cloturees(brut: list[list], maintenant_ms: int) -> list[list]:
    """Ecarte la bougie en cours : son close_time (index 6) est dans le futur."""
    return [bougie for bougie in brut if bougie[6] < maintenant_ms]


def archiver_brut(brut: list[list], symbole: str, interval: str) -> str:
    """Un document MongoDB par collecte, cle metier = premiere bougie.

    upsert : si Airflow relance la tache, le document est remplace, pas
    duplique. La reference renvoyee est celle que portera chaque bougie
    PostgreSQL issue de ce document.
    """
    premiere = brut[0][0]
    cle = {"symbol": symbole, "interval": interval, "source": SOURCE,
           "first_open_time": premiere}
    document = {
        **cle,
        # L'index unique (symbol, interval, batch_index) date de l'etape 2, ou
        # les lots etaient numerotes 0, 1, 2... Ici, le numero de lot est
        # l'ouverture de sa premiere bougie (en ms) : unique par serie, sans
        # collision possible avec les petits numeros du chargement initial.
        # Sans lui, le deuxieme document d'une serie heurtait le premier
        # (deux batch_index vides).
        "batch_index": premiere,
        "fetched_at": pd.Timestamp.now(tz="UTC").to_pydatetime(),
        "endpoint": "/api/v3/klines",
        "last_open_time": brut[-1][0],
        "candle_count": len(brut),
        "payload": brut,
    }
    with mongo_client() as client:
        client[config.MONGO_DB_NAME]["raw_klines"].replace_one(cle, document, upsert=True)
    return f"raw_klines/{symbole}/{interval}/{premiere}"


def ingerer(client: BinanceClient, conn, symbole: str, interval: str) -> int:
    table = target_table(conn, interval)
    derniere = derniere_bougie(conn, table, symbole, interval)
    debut = debut_de_collecte(derniere, interval)
    maintenant_ms = int(time.time() * 1000)
    if debut.value // 1_000_000 >= maintenant_ms:
        return 0

    brut = garder_cloturees(client.fetch_klines_history(symbole, interval, debut), maintenant_ms)
    if not brut:
        return 0

    reference = archiver_brut(brut, symbole, interval)
    bougies = deduplicate(normalize_klines(brut, symbole, interval)).assign(
        source="REST", raw_ref=reference)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingestion_runs
                (profile, symbol, interval, target_table, source, raw_collection,
                 rows_read, rows_inserted, period_start, period_end, status, finished_at)
            VALUES (%s, %s, %s, %s, 'REST', 'raw_klines', %s, %s, %s, %s, 'success', now())
            """,
            (owning_profile(conn, interval), symbole, interval, table, len(brut), len(bougies),
             bougies["open_time"].min(), bougies["open_time"].max()),
        )
        upsert_candles(cur, table, list(bougies[CANDLE_COLUMNS].itertuples(index=False, name=None)))
    conn.commit()
    log.info("%-8s %-3s : %4d bougies (%s -> %s)", symbole, interval, len(bougies),
             bougies["open_time"].min(), bougies["open_time"].max())
    return len(bougies)


def main():
    parser = argparse.ArgumentParser(description="Ingestion continue des bougies")
    parser.add_argument("--pairs", nargs="+", default=config.PAIRS)
    parser.add_argument("--intervals", nargs="+", default=INTERVALLES)
    args = parser.parse_args()

    client = BinanceClient()
    total, echecs = 0, []
    with postgres_connection() as conn:
        for interval in args.intervals:
            for symbole in args.pairs:
                try:
                    total += ingerer(client, conn, symbole, interval)
                except Exception as exc:
                    # Une paire en echec n'empeche pas les autres d'avancer ;
                    # la tache echoue quand meme a la fin, pour qu'Airflow le voie.
                    conn.rollback()
                    log.error("%s %s : echec (%s: %s)", symbole, interval, type(exc).__name__, exc)
                    echecs.append(f"{symbole} {interval}")

    log.info("Ingestion terminee : %d bougies ecrites, %d echec(s)", total, len(echecs))
    if echecs:
        raise SystemExit(f"Echec de l'ingestion pour : {', '.join(echecs)}")


if __name__ == "__main__":
    main()
