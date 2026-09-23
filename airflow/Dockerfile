# Image Airflow de CryptoBot : l'ordonnanceur ET le code qu'il fait tourner.
#
# DEUX ENVIRONNEMENTS PYTHON DANS UNE IMAGE
#   - celui d'Airflow, fourni par l'image officielle, qu'on ne touche pas ;
#   - celui du projet (/opt/airflow/projet-venv), avec scikit-learn, MLflow,
#     ta... aux versions exactes de requirements-pipeline.txt.
#
#   Airflow ORCHESTRE, il ne calcule pas : chaque tache lance une commande
#   du projet (python -m scripts.xxx) avec le Python du projet. Ce sont les
#   memes commandes qu'on lance a la main : un DAG n'est qu'un calendrier et
#   un ordre d'execution. Les deux environnements ne peuvent pas entrer en
#   conflit, et on met Airflow a jour sans risquer de casser le modele.
#
# Le code est COPIE dans l'image (pas monte depuis le disque) : l'image
# construite par la CI est exactement ce qui tourne. Seules les donnees
# (data/) et les modeles (models/) sont montes, car ils changent sans que le
# code change.
FROM apache/airflow:3.3.2-python3.13

ENV CRYPTOBOT_PROJET=/opt/airflow/projet \
    CRYPTOBOT_PYTHON=/opt/airflow/projet-venv/bin/python \
    PYTHONUNBUFFERED=1

COPY --chown=airflow:root requirements-pipeline.txt /tmp/requirements-pipeline.txt
RUN python -m venv /opt/airflow/projet-venv \
    && /opt/airflow/projet-venv/bin/pip install --no-cache-dir -r /tmp/requirements-pipeline.txt

COPY --chown=airflow:root src/ /opt/airflow/projet/src/
COPY --chown=airflow:root api/ /opt/airflow/projet/api/
COPY --chown=airflow:root scripts/ /opt/airflow/projet/scripts/
# Dossiers de travail : docs/ recoit les historiques JSON des scripts, data/
# et models/ sont remplaces par les volumes montes au demarrage.
RUN mkdir -p /opt/airflow/projet/docs /opt/airflow/projet/data /opt/airflow/projet/models

COPY --chown=airflow:root airflow/dags/ /opt/airflow/dags/
