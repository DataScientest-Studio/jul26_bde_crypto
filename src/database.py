"""Connexions aux deux bases du projet.

Un seul endroit sait ou vivent PostgreSQL et MongoDB. Les identifiants
viennent de l'environnement, avec des valeurs par defaut alignees sur
docker-compose.yml pour que `docker compose up` puis `python -m
scripts.load_to_db` fonctionne sans configuration prealable.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# --- PostgreSQL ------------------------------------------------------------
PG_HOST = os.getenv("POSTGRES_HOST", "localhost")
PG_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
PG_DB = os.getenv("POSTGRES_DB", "cryptobot")
PG_USER = os.getenv("POSTGRES_USER", "cryptobot")
PG_PASSWORD = os.getenv("POSTGRES_PASSWORD", "cryptobot")

# --- MongoDB ---------------------------------------------------------------
MONGO_HOST = os.getenv("MONGO_HOST", "localhost")
MONGO_PORT = int(os.getenv("MONGO_PORT", "27017"))
MONGO_DB = os.getenv("MONGO_DB", "cryptobot")
MONGO_USER = os.getenv("MONGO_USER", "cryptobot")
MONGO_PASSWORD = os.getenv("MONGO_PASSWORD", "cryptobot")


def postgres_dsn() -> str:
    """Chaine de connexion PostgreSQL."""
    return f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}"


def mongo_uri() -> str:
    """URI MongoDB. authSource=admin car l'utilisateur root est cree la."""
    return (
        f"mongodb://{MONGO_USER}:{MONGO_PASSWORD}@{MONGO_HOST}:{MONGO_PORT}/"
        f"?authSource=admin"
    )


@contextmanager
def postgres_connection(autocommit: bool = False):
    """Connexion PostgreSQL, fermee proprement quoi qu'il arrive.

    En cas d'exception on annule la transaction : mieux vaut aucune donnee
    qu'une ingestion a moitie ecrite, impossible a distinguer d'une bonne.
    """
    import psycopg2

    conn = psycopg2.connect(postgres_dsn())
    conn.autocommit = autocommit
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def mongo_client():
    """Client MongoDB, ferme proprement."""
    from pymongo import MongoClient

    client = MongoClient(mongo_uri(), serverSelectionTimeoutMS=5000)
    try:
        yield client
    finally:
        client.close()


def check_connections() -> dict[str, str]:
    """Teste les deux bases et retourne leur etat.

    Appele avant toute ingestion : echouer ici avec un message clair vaut
    mieux que planter au milieu de 1,5 million d'insertions.
    """
    status: dict[str, str] = {}

    try:
        with postgres_connection(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0].split(",")[0]
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
            row = cur.fetchone()
            timescale = f" + TimescaleDB {row[0]}" if row else " (sans TimescaleDB)"
            status["postgres"] = f"OK - {version}{timescale}"
    except Exception as exc:
        status["postgres"] = f"ECHEC - {type(exc).__name__}: {str(exc)[:120]}"

    try:
        with mongo_client() as client:
            info = client.server_info()
            names = client[MONGO_DB].list_collection_names()
            status["mongo"] = (
                f"OK - MongoDB {info['version']} - "
                f"{len(names)} collections : {', '.join(sorted(names)) or 'aucune'}"
            )
    except Exception as exc:
        status["mongo"] = f"ECHEC - {type(exc).__name__}: {str(exc)[:120]}"

    return status
