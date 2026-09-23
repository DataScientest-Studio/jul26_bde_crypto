"""Controle qualite des donnees fraichement collectees.

L'equivalent, pour les DONNEES, de ce que les tests unitaires sont pour le
code : une verification automatique a chaque passage du pipeline. Un modele
nourri avec des bougies manquantes ou incoherentes produit des predictions
fausses sans jamais lever d'erreur - c'est ce silence que ce controle brise.

Trois verifications par paire et pas de temps, sur les derniers jours :
    fraicheur    la derniere bougie en base a-t-elle moins de 3 intervalles ?
    completude   manque-t-il des bougies dans la fenetre ?
    coherence    haut >= bas, cloture et ouverture dans [bas, haut], volume >= 0

Chaque resultat est ecrit dans la table controles_qualite (lue par Grafana).
La commande echoue si un controle est en ECHEC : Airflow marque alors la tache
en rouge, ce qui se voit et declenche ses nouvelles tentatives.

Usage :
    python -m scripts.controle_qualite
    python -m scripts.controle_qualite --jours 7
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src import config
from src.database import postgres_connection
from src.preprocessing import interval_to_timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("qualite")

INTERVALLES = config.TRADING_PROFILES["day_trading"]["intervals"]
# Au-dela de 3 intervalles de retard, la collecte ne suit plus (une bougie
# 15 min peut legitimement arriver jusqu'a 15 min apres sa cloture, plus
# le delai entre deux passages d'Airflow).
RETARD_TOLERE = 3


def controler(bougies: pd.DataFrame, interval: str, debut: pd.Timestamp,
              maintenant: pd.Timestamp) -> dict:
    """Resultat du controle d'une serie (une paire, un pas de temps).

    Fonction pure, sans base de donnees : c'est elle que les tests verifient.
    """
    pas = interval_to_timedelta(interval)
    if bougies.empty:
        return {"derniere_bougie": None, "retard_minutes": None, "bougies_manquantes": 0,
                "bougies_incoherentes": 0, "statut": "echec"}

    derniere = bougies["open_time"].max()
    # Retard mesure depuis la CLOTURE de la derniere bougie.
    retard = maintenant - (derniere + pas)

    # Completude : toutes les ouvertures attendues entre le debut de la
    # fenetre et la derniere bougie connue.
    attendues = pd.date_range(debut.ceil(pas), derniere, freq=pas)
    manquantes = len(attendues.difference(pd.DatetimeIndex(bougies["open_time"])))

    incoherentes = int((
        (bougies["high"] < bougies["low"])
        | (bougies["close"] > bougies["high"]) | (bougies["close"] < bougies["low"])
        | (bougies["open"] > bougies["high"]) | (bougies["open"] < bougies["low"])
        | (bougies["volume"] < 0)
    ).sum())

    if incoherentes or retard > pas * RETARD_TOLERE * 4:
        statut = "echec"
    elif manquantes or retard > pas * RETARD_TOLERE:
        statut = "alerte"
    else:
        statut = "ok"
    return {"derniere_bougie": derniere, "retard_minutes": round(retard.total_seconds() / 60, 1),
            "bougies_manquantes": manquantes, "bougies_incoherentes": incoherentes,
            "statut": statut}


def main():
    parser = argparse.ArgumentParser(description="Controle qualite des bougies recentes")
    parser.add_argument("--pairs", nargs="+", default=config.PAIRS)
    parser.add_argument("--jours", type=int, default=2)
    args = parser.parse_args()

    maintenant = pd.Timestamp.now(tz="UTC")
    debut = maintenant - pd.Timedelta(days=args.jours)
    echecs = []

    with postgres_connection() as conn:
        bougies = pd.read_sql(
            """SELECT symbol, interval, open_time, open, high, low, close, volume
                 FROM v_day_trading
                WHERE open_time >= %(debut)s AND symbol = ANY(%(paires)s)""",
            conn, params={"debut": debut.to_pydatetime(), "paires": args.pairs})

        with conn.cursor() as cur:
            for interval in INTERVALLES:
                for symbole in args.pairs:
                    serie = bougies[(bougies["symbol"] == symbole) & (bougies["interval"] == interval)]
                    resultat = controler(serie, interval, debut, maintenant)
                    cur.execute(
                        """INSERT INTO controles_qualite
                               (symbol, interval, derniere_bougie, retard_minutes,
                                bougies_manquantes, bougies_incoherentes, statut)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (symbole, interval, resultat["derniere_bougie"], resultat["retard_minutes"],
                         resultat["bougies_manquantes"], resultat["bougies_incoherentes"],
                         resultat["statut"]))
                    niveau = logging.INFO if resultat["statut"] == "ok" else logging.WARNING
                    log.log(niveau, "%-8s %-3s %-6s retard %s min | manquantes %d | incoherentes %d",
                            symbole, interval, resultat["statut"].upper(), resultat["retard_minutes"],
                            resultat["bougies_manquantes"], resultat["bougies_incoherentes"])
                    if resultat["statut"] == "echec":
                        echecs.append(f"{symbole} {interval}")

    if echecs:
        raise SystemExit(f"Controle qualite en echec : {', '.join(echecs)}")
    log.info("Controle qualite : tout est en ordre.")


if __name__ == "__main__":
    main()
