"""Calcule les variables techniques et les stocke dans les deux bases.

    PostgreSQL  technical_features   -> les VALEURS, une ligne par bougie
    MongoDB     feature_configs      -> la RECETTE qui les a produites

Pourquoi ce partage plutot que tout mettre des deux cotes : la separation
des couches defendue a l'etape 2 reste la meme. PostgreSQL stocke ce qui
est tabulaire, dense et interroge en masse ; MongoDB stocke ce qui est
polymorphe et rare - ici la configuration d'un calcul, dont les cles
changent d'un jeu de variables a l'autre (familles, fenetres, contexte).

Dupliquer les valeurs dans MongoDB n'apporterait rien : ce sont des
nombres, tous de la meme forme, exactement ce qu'une base relationnelle
fait le mieux.

Les bougies sont lues depuis les VUES par profil, jamais depuis les tables :
le calcul n'a pas a savoir ou chaque pas de temps reside physiquement.

Usage :
    python -m scripts.load_features                       # day_trading, jeu de base
    python -m scripts.load_features --contexte            # avec les pas de temps lents
    python -m scripts.load_features --profils swing
    python -m scripts.load_features --etat                # ce qui est deja en base

Volume : compter environ 1 Go pour le scalping (1,6 M de bougies). Le
day trading, cible actuelle du projet, tient dans ~300 Mo.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src import config
from src.database import mongo_client, postgres_connection
from src.features import (
    CONTEXTE, FAMILLES, ajouter_contexte_lent, construire_groupes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("features")

VUES = {"scalping": "v_scalping", "day_trading": "v_day_trading", "swing": "v_swing"}

# Colonnes des bougies necessaires au calcul des indicateurs.
COLONNES_BOUGIES = [
    "symbol", "interval", "open_time", "open", "high", "low", "close",
    "volume", "quote_volume", "nb_trades", "taker_buy_base",
]


def identifiant(contexte: bool) -> str:
    """Nom du jeu de variables. Change des que la recette change."""
    return "contexte_v1" if contexte else "base_v1"


def enregistrer_configuration(config_ref: str, contexte: bool, colonnes: list[str]) -> None:
    """Ecrit la recette dans MongoDB.

    C'est ce document qui permet, dans six mois, de savoir ce que
    contenait exactement "contexte_v1" - et de refuser de melanger deux
    jeux qui portent le meme nom sans avoir le meme contenu.
    """
    document = {
        "_id": config_ref,
        "familles": list(FAMILLES),
        "contexte_multi_echelles": contexte,
        "colonnes_de_contexte": list(CONTEXTE) if contexte else [],
        "nb_variables": len(colonnes),
        "colonnes": colonnes,
        "module": "src/features.py",
        "enregistre_le": datetime.now(timezone.utc),
    }
    with mongo_client() as client:
        client[config.MONGO_DB_NAME]["feature_configs"].replace_one(
            {"_id": config_ref}, document, upsert=True
        )
    log.info("MongoDB  feature_configs/%s : %d variables", config_ref, len(colonnes))


def lire_bougies(conn, profil: str, pairs: list[str] | None) -> pd.DataFrame:
    """Charge les bougies du profil depuis sa vue."""
    requete = f"SELECT {', '.join(COLONNES_BOUGIES)} FROM {VUES[profil]}"
    parametres: list = []
    if pairs:
        requete += " WHERE symbol = ANY(%s)"
        parametres.append(pairs)
    requete += " ORDER BY symbol, interval, open_time"

    df = pd.read_sql(requete, conn, params=parametres or None)
    log.info("%s : %d bougies lues dans %s", profil, len(df), VUES[profil])
    return df


def calculer(brut: pd.DataFrame, contexte: bool) -> pd.DataFrame:
    variables = construire_groupes(brut, FAMILLES)
    if contexte:
        variables = ajouter_contexte_lent(variables)
    return variables


def ecrire(conn, variables: pd.DataFrame, config_ref: str) -> int:
    """Insere les variables en JSONB, par lots.

    Idempotent : relancer le calcul met a jour les lignes existantes au
    lieu d'echouer ou d'empiler des doublons.
    """
    from psycopg2.extras import Json, execute_values

    cle = ["symbol", "interval", "open_time"]
    colonnes = [c for c in variables.columns if c not in cle]

    # Deux conversions indispensables :
    #   - float() explicite, car psycopg2 serialise le JSONB avec json.dumps,
    #     qui refuse les numpy.float64 ;
    #   - NaN -> None, car NaN n'existe pas en JSON. Les indicateurs a
    #     fenetre longue en produisent sur les premieres bougies.
    brutes = variables[colonnes].to_numpy(dtype=float)

    lignes = [
        (row.symbol, row.interval, row.open_time, config_ref,
         Json({c: (None if np.isnan(v) else float(v)) for c, v in zip(colonnes, vals)}))
        for row, vals in zip(variables[cle].itertuples(index=False), brutes)
    ]

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO technical_features
                (symbol, interval, open_time, config_ref, valeurs)
            VALUES %s
            ON CONFLICT (symbol, interval, open_time, config_ref) DO UPDATE SET
                valeurs     = EXCLUDED.valeurs,
                computed_at = now()
            """,
            lignes,
            page_size=2000,
        )
        # Comme pour les bougies : apres execute_values, rowcount ne compte
        # que le dernier lot. On interroge la table.
        cur.execute(
            "SELECT count(*) FROM technical_features WHERE config_ref = %s", (config_ref,)
        )
        return cur.fetchone()[0]


def afficher_etat(conn) -> None:
    """Ce qui est deja calcule, via la vue de supervision."""
    df = pd.read_sql("SELECT * FROM v_features_coverage ORDER BY config_ref, symbol, interval",
                     conn)
    if df.empty:
        log.info("Aucune variable technique en base pour le moment.")
        return
    print()
    print(df.to_string(index=False))
    print()


def main():
    parser = argparse.ArgumentParser(description="Calcul et stockage des variables techniques")
    parser.add_argument("--profils", nargs="+", default=["day_trading"], choices=list(VUES))
    parser.add_argument("--pairs", nargs="+", default=None)
    parser.add_argument("--contexte", action="store_true",
                        help="Ajouter l'etat des pas de temps plus lents")
    parser.add_argument("--etat", action="store_true",
                        help="Afficher ce qui est deja en base et sortir")
    args = parser.parse_args()

    config_ref = identifiant(args.contexte)
    debut = time.time()

    with postgres_connection() as conn:
        if args.etat:
            afficher_etat(conn)
            return

        for profil in args.profils:
            brut = lire_bougies(conn, profil, args.pairs)
            if brut.empty:
                log.warning("%s : aucune bougie, rien a calculer", profil)
                continue

            variables = calculer(brut, args.contexte)
            colonnes = [c for c in variables.columns
                        if c not in ("symbol", "interval", "open_time")]

            # La recette est enregistree AVANT les valeurs : si l'ecriture
            # des valeurs echoue, on sait au moins ce qui etait tente.
            enregistrer_configuration(config_ref, args.contexte, colonnes)

            total = ecrire(conn, variables, config_ref)
            conn.commit()
            log.info("%s : %d lignes calculees (%d variables) | %d en base pour %s",
                     profil, len(variables), len(colonnes), total, config_ref)

        afficher_etat(conn)

    log.info("Termine en %.0f s", time.time() - debut)


if __name__ == "__main__":
    main()
