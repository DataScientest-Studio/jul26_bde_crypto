"""Toutes les 15 minutes : collecter, verifier, calculer, faire tourner le bot.

    ingerer_bougies ──┬── controler_qualite
                      └── calculer_variables
    faire_tourner_le_bot          (independant de la base)

POURQUOI 1, 16, 31, 46 ET PAS 0, 15, 30, 45
    Une bougie 15 minutes se cloture a :14:59. Lancer a la minute pile, c'est
    risquer d'arriver avant sa cloture et de la rater jusqu'au passage
    suivant. Une minute de marge suffit.

LE BOT TOURNE ICI, PLUS DANS LA PAGE
    Jusqu'a l'etape 4, le carnet de positions n'avancait que lorsqu'on
    ouvrait l'interface. Desormais Airflow appelle l'API toutes les 15
    minutes pour chaque paire, pas de temps et style : le bot tourne en
    continu, qu'on le regarde ou non. Il passe par l'API (avec SA cle), comme
    n'importe quel client : un seul chemin de code pour le bot, qu'il soit
    declenche par Airflow ou par l'interface.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import dag, task

from cryptobot_commun import PARAMETRES_PAR_DEFAUT, commande

log = logging.getLogger(__name__)

PAIRES = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]
INTERVALLES = ["15m", "1h", "4h"]
STYLES = ["conservateur", "agressif"]


@dag(
    dag_id="cryptobot_collecte",
    description="Bougies Binance -> MongoDB + PostgreSQL, controle qualite, variables, bot",
    schedule="1-59/15 * * * *",
    start_date=datetime(2026, 9, 1),
    # Pas de rattrapage des passages manques : l'ingestion reprend d'elle-meme
    # a la derniere bougie en base, un seul passage suffit a combler un trou.
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=14),
    default_args=PARAMETRES_PAR_DEFAUT,
    tags=["cryptobot", "production"],
)
def cryptobot_collecte():
    ingerer = BashOperator(
        task_id="ingerer_bougies",
        bash_command=commande("ingestion_continue"),
        execution_timeout=timedelta(minutes=8),
    )
    controler = BashOperator(
        task_id="controler_qualite",
        bash_command=commande("controle_qualite", "--jours", "2"),
        execution_timeout=timedelta(minutes=3),
        # Un controle en echec est un SIGNAL, pas une panne passagere :
        # reessayer ne changerait rien au resultat.
        retries=0,
    )
    variables = BashOperator(
        task_id="calculer_variables",
        bash_command=commande("load_features", "--contexte", "--jours", "2"),
        execution_timeout=timedelta(minutes=8),
    )

    @task(execution_timeout=timedelta(minutes=10))
    def faire_tourner_le_bot() -> dict:
        """Un tour de carnet pour chaque paire, pas de temps et style."""
        import requests

        adresse = os.getenv("CRYPTOBOT_API_URL", "http://api:8000")
        entetes = {"X-API-Key": os.environ["CRYPTOBOT_CLE_AIRFLOW"]}
        bilan, echecs = {"tours": 0, "positions_ouvertes": 0, "positions_fermees": 0}, []
        for paire in PAIRES:
            for interval in INTERVALLES:
                for style in STYLES:
                    try:
                        reponse = requests.post(
                            f"{adresse}/positions/{paire}",
                            params={"interval": interval, "style": style, "source": "binance"},
                            headers=entetes, timeout=120)
                        reponse.raise_for_status()
                        corps = reponse.json()
                        bilan["tours"] += 1
                        bilan["positions_ouvertes"] += int(corps["mouvement"]["position_ouverte"])
                        bilan["positions_fermees"] += corps["mouvement"]["positions_fermees"]
                    except Exception as exc:
                        log.error("%s %s %s : %s", paire, interval, style, exc)
                        echecs.append(f"{paire} {interval} {style}")
        log.info("Bot : %s", bilan)
        if echecs:
            raise RuntimeError(f"{len(echecs)} tour(s) en echec : {', '.join(echecs)}")
        return bilan

    ingerer >> [controler, variables]
    faire_tourner_le_bot()


cryptobot_collecte()
