"""Chaque matin : mesurer la derive, et reentrainer si elle devient forte.

    mesurer_derive >> derive_forte_et_pas_de_reentrainement_recent >> declencher_reentrainement

La derive (PSI, voir api/derive.py) compare les 90 derniers jours aux donnees
d'apprentissage du modele EN SERVICE. Quand trop de variables s'en eloignent,
le modele raisonne sur un marche qu'il n'a pas connu : on declenche le DAG de
reentrainement sans attendre dimanche.

GARDE-FOU
    Une derive forte ne disparait pas en un jour. Sans limite, elle
    declencherait un reentrainement CHAQUE matin. On attend donc au moins
    ECART_MINIMUM depuis le dernier duel : le temps d'accumuler des donnees
    vraiment nouvelles.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sdk import dag, task

from cryptobot_commun import PARAMETRES_PAR_DEFAUT, commande

log = logging.getLogger(__name__)

# Nombre de groupes (paire x pas de temps, 15 au total) en derive forte a
# partir duquel on reentraine.
GROUPES_EN_DERIVE_FORTE = 8
ECART_MINIMUM = timedelta(days=3)


@dag(
    dag_id="cryptobot_derive",
    description="Mesure quotidienne de la derive ; declenche le reentrainement si elle est forte",
    schedule="30 6 * * *",
    start_date=datetime(2026, 9, 1),
    catchup=False,
    max_active_runs=1,
    default_args=PARAMETRES_PAR_DEFAUT,
    tags=["cryptobot", "surveillance"],
)
def cryptobot_derive():
    mesurer = BashOperator(
        task_id="mesurer_derive",
        bash_command=commande("mesurer_derive", "--enregistrer", "--jours", "90"),
        execution_timeout=timedelta(minutes=20),
    )

    @task.short_circuit
    def derive_forte_et_pas_de_reentrainement_recent() -> bool:
        """Vrai = on continue vers le reentrainement ; faux = on s'arrete la."""
        from airflow.providers.postgres.hooks.postgres import PostgresHook

        base = PostgresHook(postgres_conn_id="cryptobot_postgres")
        groupes = base.get_first("""
            SELECT count(*) FROM derive_mesures
             WHERE mesure_le = (SELECT max(mesure_le) FROM derive_mesures)
               AND variables_en_derive_forte > 0""")[0]
        dernier = base.get_first("SELECT max(lance_le) FROM reentrainements")[0]
        recent = dernier is not None and datetime.now(dernier.tzinfo) - dernier < ECART_MINIMUM
        log.info("%d groupe(s) en derive forte (seuil %d) ; dernier reentrainement : %s",
                 groupes, GROUPES_EN_DERIVE_FORTE, dernier)
        if recent:
            log.info("Reentrainement trop recent : on laisse les donnees s'accumuler.")
        return groupes >= GROUPES_EN_DERIVE_FORTE and not recent

    declencher = TriggerDagRunOperator(
        task_id="declencher_reentrainement",
        trigger_dag_id="cryptobot_reentrainement",
        conf={"declenche_par": "derive"},
    )

    mesurer >> derive_forte_et_pas_de_reentrainement_recent() >> declencher


cryptobot_derive()
