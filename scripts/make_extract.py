"""Extrait fige des donnees, pour que le travail de ML soit reproductible.

Remy l'a demande explicitement : travailler sur un extrait fige evite que
les resultats bougent parce que des donnees sont arrivees entre deux
executions. Un modele dont le score change sans qu'on ait touche au code
est un modele qu'on ne peut ni comparer ni defendre.

Ce que "fige" veut dire ici :
  - une date de coupure ecrite dans le manifeste, jamais "maintenant"
  - une empreinte SHA-256 par fichier
  - le tout relu et verifie avant chaque entrainement

Usage :
    python -m scripts.make_extract                    # cree l'extrait
    python -m scripts.make_extract --verify           # controle les empreintes
    python -m scripts.make_extract --cutoff 2026-08-01
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src import config
from src.database import postgres_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("extract")

EXTRACT_DIR = config.ROOT / "data" / "extract"
MANIFEST = EXTRACT_DIR / "manifest.json"


def empreinte(chemin: Path) -> str:
    """SHA-256 du fichier, lue par blocs pour ne pas charger 400 Mo en memoire."""
    h = hashlib.sha256()
    with chemin.open("rb") as f:
        for bloc in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloc)
    return h.hexdigest()


def derniere_bougie_commune(conn) -> pd.Timestamp:
    """Date de coupure par defaut : la derniere bougie presente PARTOUT.

    Prendre le maximum global donnerait une coupure que les pas de temps
    lents (1w) n'ont pas atteinte. On prend donc le minimum des maximums :
    au-dela, certaines series seraient tronquees et d'autres non.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT min(last_candle) FROM v_coverage")
        return pd.Timestamp(cur.fetchone()[0])


def extraire(conn, profile: str, cutoff: pd.Timestamp) -> pd.DataFrame:
    """Lit une vue de profil jusqu'a la date de coupure.

    On lit la VUE et non la table : le profil recupere ainsi ses trois pas
    de temps, y compris celui qu'il ne stocke pas lui-meme.
    """
    requete = f"""
        SELECT symbol, interval, open_time, close_time,
               open, high, low, close,
               volume, quote_volume, nb_trades,
               taker_buy_base, taker_buy_quote
          FROM v_{profile}
         WHERE open_time <= %(cutoff)s
         ORDER BY symbol, interval, open_time
    """
    return pd.read_sql(requete, conn, params={"cutoff": cutoff.to_pydatetime()})


def creer(args):
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)

    with postgres_connection(autocommit=True) as conn:
        cutoff = (pd.Timestamp(args.cutoff, tz="UTC") if args.cutoff
                  else derniere_bougie_commune(conn))
        log.info("Date de coupure : %s", cutoff)

        manifeste = {
            "cree_le": datetime.now(timezone.utc).isoformat(),
            "cutoff": cutoff.isoformat(),
            "source": "PostgreSQL / TimescaleDB, vues par profil",
            "pourquoi": "Extrait fige : les resultats de ML doivent etre "
                        "reproductibles a l'identique.",
            "profils": {},
        }

        for profile in config.TRADING_PROFILES:
            df = extraire(conn, profile, cutoff)
            if df.empty:
                log.warning("%s : aucune donnee", profile)
                continue

            chemin = EXTRACT_DIR / f"{profile}.parquet"
            df.to_parquet(chemin, index=False)

            manifeste["profils"][profile] = {
                "fichier": chemin.name,
                "lignes": len(df),
                "sha256": empreinte(chemin),
                "taille_ko": round(chemin.stat().st_size / 1024),
                "pas_de_temps": sorted(df["interval"].unique().tolist()),
                "paires": sorted(df["symbol"].unique().tolist()),
                "debut": str(df["open_time"].min()),
                "fin": str(df["open_time"].max()),
            }
            log.info("%-12s %9d lignes | %s | %s ko",
                     profile, len(df),
                     ", ".join(sorted(df["interval"].unique())),
                     manifeste["profils"][profile]["taille_ko"])

    MANIFEST.write_text(json.dumps(manifeste, indent=2), encoding="utf-8")
    total = sum(p["lignes"] for p in manifeste["profils"].values())
    log.info("Manifeste ecrit : %s (%d lignes au total)", MANIFEST, total)


def verifier():
    """Recalcule les empreintes et les compare au manifeste.

    A appeler avant chaque entrainement : si un fichier a bouge, les
    resultats ne sont plus comparables aux precedents.
    """
    if not MANIFEST.exists():
        log.error("Aucun manifeste. Lancer d'abord : python -m scripts.make_extract")
        sys.exit(1)

    manifeste = json.loads(MANIFEST.read_text(encoding="utf-8"))
    log.info("Extrait du %s, coupure %s", manifeste["cree_le"][:19], manifeste["cutoff"])

    tout_va_bien = True
    for profile, meta in manifeste["profils"].items():
        chemin = EXTRACT_DIR / meta["fichier"]
        if not chemin.exists():
            log.error("%-12s FICHIER ABSENT", profile)
            tout_va_bien = False
            continue
        actuelle = empreinte(chemin)
        if actuelle == meta["sha256"]:
            log.info("%-12s OK   %9d lignes  %s", profile, meta["lignes"], actuelle[:16])
        else:
            log.error("%-12s MODIFIE : attendu %s, obtenu %s",
                      profile, meta["sha256"][:16], actuelle[:16])
            tout_va_bien = False

    if not tout_va_bien:
        sys.exit(1)
    log.info("Extrait intact : les resultats restent comparables.")


def main():
    parser = argparse.ArgumentParser(description="Extrait fige pour le ML")
    parser.add_argument("--cutoff", help="Date de coupure (AAAA-MM-JJ)")
    parser.add_argument("--verify", action="store_true",
                        help="Verifier les empreintes sans rien recreer")
    args = parser.parse_args()

    verifier() if args.verify else creer(args)


if __name__ == "__main__":
    main()
