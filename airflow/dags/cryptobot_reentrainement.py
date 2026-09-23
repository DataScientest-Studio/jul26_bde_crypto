"""Chaque dimanche (ou sur derive forte) : le duel champion / challenger.

    figer_extrait >> entrainer_et_comparer >> challenger_promu >> publier
                                                                    │
    nettoyer_anciens_extraits  <────────────────────────────────────┘ (toujours)

Le detail des regles est dans scripts/reentrainer.py. Ce DAG garantit
l'ORDRE et l'ISOLEMENT :
  - chaque reentrainement a son identifiant (date et heure de lancement) et
    son propre dossier : deux executions ne se melangent jamais ;
  - la publication n'a lieu que si le duel l'a decide ;
  - le modele en service n'est remplace qu'a la toute fin, en une operation
    atomique : si une etape echoue avant, la production n'a rien vu.

Dimanche 3 h UTC : le marche crypto ne ferme jamais, mais c'est le creux
d'activite de la semaine, et l'entrainement occupe plusieurs coeurs pendant
quelques minutes.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import dag, task

from cryptobot_commun import PARAMETRES_PAR_DEFAUT, PROJET, commande

log = logging.getLogger(__name__)

DOSSIER = Path(PROJET) / "data" / "reentrainement"
# Identifiant du reentrainement = moment ou le run a ete lance. run_after
# existe toujours (logical_date peut etre vide pour un lancement manuel), et
# ce format n'a pas de ":", interdit dans un nom de dossier sous Windows.
IDENTIFIANT = "{{ dag_run.run_after.strftime('%Y-%m-%dT%H%M%S') }}"
EXTRAITS_CONSERVES = 4


@dag(
    dag_id="cryptobot_reentrainement",
    description="Extrait fige -> challenger -> duel sur 21 jours jamais vus -> publication si meilleur",
    schedule="0 3 * * 0",
    start_date=datetime(2026, 9, 1),
    catchup=False,
    max_active_runs=1,
    default_args={**PARAMETRES_PAR_DEFAUT, "retries": 1},
    tags=["cryptobot", "machine-learning"],
)
def cryptobot_reentrainement():
    figer = BashOperator(
        task_id="figer_extrait",
        bash_command=commande("reentrainer", "figer", "--date", IDENTIFIANT),
        execution_timeout=timedelta(minutes=15),
    )
    entrainer = BashOperator(
        task_id="entrainer_et_comparer",
        bash_command=commande("reentrainer", "entrainer", "--date", IDENTIFIANT),
        execution_timeout=timedelta(hours=2),
    )

    @task.short_circuit(ignore_downstream_trigger_rules=False)
    def challenger_promu(identifiant: str) -> bool:
        decision = json.loads((DOSSIER / identifiant / "decision.json").read_text(encoding="utf-8"))
        log.info("Duel %s : %s (%s)", identifiant, decision["decision"], decision["raison"])
        return decision["decision"] == "promu"

    publier = BashOperator(
        task_id="publier",
        bash_command=commande("reentrainer", "publier", "--date", IDENTIFIANT),
        execution_timeout=timedelta(minutes=20),
    )

    @task(trigger_rule="all_done")
    def nettoyer_anciens_extraits() -> list[str]:
        """Garde les derniers extraits : chacun pese plusieurs dizaines de Mo.

        Les modeles, eux, restent tous dans MLflow et dans models/archives/.
        """
        if not DOSSIER.exists():
            return []
        dossiers = sorted((d for d in DOSSIER.iterdir() if d.is_dir()), key=os.path.getmtime)
        supprimes = []
        for ancien in dossiers[:-EXTRAITS_CONSERVES]:
            shutil.rmtree(ancien)
            supprimes.append(ancien.name)
        log.info("Extraits supprimes : %s", supprimes or "aucun")
        return supprimes

    promu = challenger_promu(IDENTIFIANT)
    figer >> entrainer >> promu >> publier >> nettoyer_anciens_extraits()


cryptobot_reentrainement()
