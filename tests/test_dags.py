"""Tests d'integrite des DAG Airflow.

Un DAG qui ne s'importe pas disparait silencieusement de l'interface
d'Airflow : la collecte s'arrete et personne ne le voit. Ces tests chargent
les DAG exactement comme le fait Airflow et verifient leur structure.

Ils demandent Airflow installe : ils tournent dans la CI (job dedie) et
sont ignores ailleurs.

Lancement :
    pytest tests/test_dags.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

# "airflow.models" et non "airflow" : le dossier airflow/ du depot serait pris
# pour le paquet, et le test ne serait jamais ignore.
pytest.importorskip("airflow.models")

DOSSIER_DAGS = Path(__file__).resolve().parent.parent / "airflow" / "dags"


@pytest.fixture(scope="module")
def dagbag():
    import sys

    from airflow.models import DagBag

    sys.path.insert(0, str(DOSSIER_DAGS))
    return DagBag(dag_folder=str(DOSSIER_DAGS), include_examples=False)


def test_aucune_erreur_d_import(dagbag):
    assert dagbag.import_errors == {}


def test_les_trois_dag_sont_presents(dagbag):
    assert set(dagbag.dag_ids) == {"cryptobot_collecte", "cryptobot_derive",
                                   "cryptobot_reentrainement"}


def test_collecte_toutes_les_15_minutes_apres_cloture(dagbag):
    dag = dagbag.get_dag("cryptobot_collecte")
    assert dag.schedule == "1-59/15 * * * *"
    assert dag.catchup is False
    assert dag.max_active_runs == 1


def test_ordre_de_la_collecte(dagbag):
    dag = dagbag.get_dag("cryptobot_collecte")
    assert dag.get_task("ingerer_bougies").downstream_task_ids == {
        "controler_qualite", "calculer_variables"}
    # Le bot ne depend pas de la base : il tourne meme si l'ingestion echoue.
    assert dag.get_task("faire_tourner_le_bot").upstream_task_ids == set()


def test_reentrainement_publie_seulement_apres_le_duel(dagbag):
    dag = dagbag.get_dag("cryptobot_reentrainement")
    chemin = ["figer_extrait", "entrainer_et_comparer", "challenger_promu", "publier"]
    for amont, aval in zip(chemin, chemin[1:]):
        assert aval in dag.get_task(amont).downstream_task_ids
    assert dag.get_task("nettoyer_anciens_extraits").trigger_rule == "all_done"


def test_derive_declenche_le_reentrainement(dagbag):
    dag = dagbag.get_dag("cryptobot_derive")
    declencheur = dag.get_task("declencher_reentrainement")
    assert declencheur.trigger_dag_id == "cryptobot_reentrainement"


def test_toutes_les_taches_ont_des_nouvelles_tentatives(dagbag):
    """Sauf le controle qualite : reessayer ne change pas un constat."""
    for dag in dagbag.dags.values():
        for tache in dag.tasks:
            if tache.task_id in {"controler_qualite", "challenger_promu",
                                 "derive_forte_et_pas_de_reentrainement_recent",
                                 "declencher_reentrainement", "nettoyer_anciens_extraits"}:
                continue
            assert tache.retries >= 1, f"{dag.dag_id}.{tache.task_id} sans nouvelle tentative"


def test_les_commandes_utilisent_le_python_du_projet(dagbag):
    """Les taches tournent dans l'environnement du projet, pas celui d'Airflow."""
    for dag in dagbag.dags.values():
        for tache in dag.tasks:
            commande = getattr(tache, "bash_command", None)
            if commande:
                assert "projet-venv/bin/python -m scripts." in commande
                assert commande.startswith("set -euo pipefail")
