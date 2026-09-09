"""Etat des deux bases : ce qu'elles contiennent et si c'est coherent.

Repond a "est-ce que le chargement s'est bien passe ?" sans avoir a ouvrir
psql. Servira aussi au monitoring de l'etape 5.

Usage :
    python -m scripts.check_db
    python -m scripts.check_db --detail    # ligne par ligne
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.database import mongo_client, postgres_connection


def afficher(titre: str, colonnes: list[str], lignes: list[tuple]):
    """Petit tableau aligne, sans dependance."""
    print(f"\n{titre}")
    if not lignes:
        print("  (vide)")
        return
    largeurs = [
        max(len(str(colonnes[i])), max(len(str(l[i])) for l in lignes))
        for i in range(len(colonnes))
    ]
    print("  " + "  ".join(str(c).ljust(largeurs[i]) for i, c in enumerate(colonnes)))
    print("  " + "  ".join("-" * largeurs[i] for i in range(len(colonnes))))
    for ligne in lignes:
        print("  " + "  ".join(str(v).ljust(largeurs[i]) for i, v in enumerate(ligne)))


def main():
    parser = argparse.ArgumentParser(description="Etat des bases CryptoBot")
    parser.add_argument("--detail", action="store_true",
                        help="Detail par paire et pas de temps")
    args = parser.parse_args()

    with postgres_connection(autocommit=True) as conn, conn.cursor() as cur:

        # --- Volume par table de faits -------------------------------------
        lignes = []
        total = 0
        for profile in config.TRADING_PROFILES:
            table = f"candles_{profile}"
            cur.execute(f"SELECT count(*) FROM {table}")
            n = cur.fetchone()[0]
            total += n
            cur.execute(
                "SELECT count(*) FROM timescaledb_information.chunks "
                "WHERE hypertable_name = %s", (table,)
            )
            chunks = cur.fetchone()[0]
            cur.execute(
                "SELECT pg_size_pretty(hypertable_size(%s::regclass))", (table,)
            )
            taille = cur.fetchone()[0]
            cur.execute(
                f"SELECT string_agg(DISTINCT interval, ', ') FROM {table}"
            )
            intervalles = cur.fetchone()[0] or "-"
            lignes.append((table, intervalles, f"{n:,}", chunks, taille))
        afficher("VOLUME PAR TABLE DE FAITS",
                 ["table", "pas de temps", "lignes", "chunks", "taille"], lignes)
        print(f"\n  TOTAL : {total:,} lignes")

        # --- Les vues restituent-elles les 3 pas de temps ? ----------------
        lignes = []
        for profile, meta in config.TRADING_PROFILES.items():
            cur.execute(f"SELECT count(DISTINCT interval), count(*) FROM v_{profile}")
            distincts, n = cur.fetchone()
            attendu = len(meta["intervals"])
            statut = "OK" if distincts == attendu else f"ATTENDU {attendu}"
            lignes.append((f"v_{profile}", distincts, attendu, f"{n:,}", statut))
        afficher("LES VUES RESTITUENT-ELLES TOUS LES PAS DE TEMPS ?",
                 ["vue", "distincts", "attendu", "lignes", "statut"], lignes)

        # --- Controles d'integrite -----------------------------------------
        lignes = []
        for profile in config.TRADING_PROFILES:
            table = f"candles_{profile}"
            cur.execute(f"""
                SELECT
                  (SELECT count(*) FROM (
                     SELECT symbol, interval, open_time FROM {table}
                     GROUP BY 1,2,3 HAVING count(*) > 1) d),
                  (SELECT count(*) FROM {table} WHERE close IS NULL OR volume IS NULL),
                  (SELECT count(*) FROM {table} WHERE raw_ref IS NULL)
            """)
            doublons, nuls, sans_ref = cur.fetchone()
            lignes.append((table, doublons, nuls, sans_ref))
        afficher("INTEGRITE (tout doit etre a zero)",
                 ["table", "doublons", "valeurs nulles", "sans lien Mongo"], lignes)

        # --- Journal des collectes -----------------------------------------
        cur.execute("""
            SELECT status, count(*), sum(rows_inserted)
              FROM ingestion_runs GROUP BY status ORDER BY status
        """)
        afficher("JOURNAL DES COLLECTES",
                 ["statut", "executions", "lignes inserees"], cur.fetchall())

        # --- Retard de collecte --------------------------------------------
        cur.execute("""
            SELECT interval, count(*) AS jeux,
                   max(candles_behind) AS retard_max
              FROM v_coverage GROUP BY interval
             ORDER BY max(duration_seconds)
        """)
        afficher("RETARD DE COLLECTE (en nombre de bougies)",
                 ["pas de temps", "jeux", "retard max"], cur.fetchall())

        if args.detail:
            cur.execute("""
                SELECT symbol, interval, rows_stored, first_candle::date,
                       last_candle::date, candles_behind
                  FROM v_coverage ORDER BY interval, symbol
            """)
            afficher("DETAIL PAR JEU DE DONNEES",
                     ["paire", "pas", "lignes", "debut", "fin", "retard"],
                     cur.fetchall())

    # --- MongoDB -----------------------------------------------------------
    with mongo_client() as client:
        db = client[config.MONGO_DB_NAME]
        lignes = []
        for nom in sorted(db.list_collection_names()):
            stats = db.command("collstats", nom)
            lignes.append((
                nom,
                f"{stats['count']:,}",
                f"{stats['size'] / 1024 / 1024:.1f} Mo",
                stats.get("nindexes", 0),
            ))
        afficher("MONGODB - COUCHE BRUTE",
                 ["collection", "documents", "taille", "index"], lignes)

    print()


if __name__ == "__main__":
    main()
