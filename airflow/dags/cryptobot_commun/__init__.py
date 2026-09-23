"""Ce que les trois DAG de CryptoBot ont en commun.

Un DAG ne contient AUCUNE logique metier : il dit quoi lancer, quand, dans
quel ordre, et que faire en cas d'echec. La logique vit dans scripts/, ou
elle est testee et utilisable a la main. Ici, on ne fait que construire la
ligne de commande.
"""
from __future__ import annotations

import os
from datetime import timedelta

PROJET = os.getenv("CRYPTOBOT_PROJET", "/opt/airflow/projet")
PYTHON = os.getenv("CRYPTOBOT_PYTHON", "/opt/airflow/projet-venv/bin/python")

# Politique par defaut : trois nouvelles tentatives espacees. La plupart des
# echecs reels sont passagers (Binance qui repond 5xx, base qui redemarre).
PARAMETRES_PAR_DEFAUT = {
    "owner": "cryptobot",
    "retries": 3,
    "retry_delay": timedelta(minutes=2),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=15),
}


def commande(module: str, *arguments: str) -> str:
    """`python -m scripts.<module>` avec le Python du projet, depuis le projet.

    `set -euo pipefail` : la moindre erreur arrete la commande et la fait
    echouer, donc Airflow marque la tache en rouge au lieu de la croire reussie.
    """
    return (f"set -euo pipefail; cd {PROJET} && "
            f"{PYTHON} -m scripts.{module} {' '.join(arguments)}").strip()
