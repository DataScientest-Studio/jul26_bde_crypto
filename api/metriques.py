"""Metriques Prometheus propres a CryptoBot.

Les metriques HTTP generiques (nombre d'appels, duree, codes d'erreur par
route) sont ajoutees par prometheus-fastapi-instrumentator dans main.py. Ici,
ce qui est PROPRE au metier : ce que le bot decide, avec quel modele, et ce
qui echoue.

Prometheus vient lire /metrics toutes les 15 secondes. Cette route n'est
joignable QUE depuis le reseau Docker interne : le proxy nginx ne la
transmet pas (voir interface/nginx.conf.template).
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

DECISIONS = Counter(
    "cryptobot_decisions_total",
    "Decisions prises par le bot (une par appel de /prediction ou tour du carnet)",
    ["interval", "style", "decision"],
)

PROBABILITE_HAUSSE = Histogram(
    "cryptobot_probabilite_hausse",
    "Probabilite de hausse annoncee par le modele. Un modele qui colle a 0,5 "
    "est un modele indecis : c'est ce que montre la largeur de cette distribution.",
    buckets=(0.30, 0.35, 0.40, 0.43, 0.46, 0.48, 0.50, 0.52, 0.54, 0.57, 0.60, 0.65, 0.70),
)

APPELS_PAR_CLIENT = Counter(
    "cryptobot_appels_authentifies_total",
    "Appels acceptes, par client (interface, airflow...)",
    ["client"],
)

APPELS_REFUSES = Counter(
    "cryptobot_appels_refuses_total",
    "Appels refuses par la verification de cle",
    ["raison"],
)

ERREURS_SOURCES = Counter(
    "cryptobot_erreurs_sources_total",
    "Echecs d'acces aux donnees (Binance, PostgreSQL, MongoDB)",
    ["source"],
)

POSITIONS_RATTRAPEES = Counter(
    "cryptobot_positions_rattrapees_total",
    "Positions ajoutees par le rattrapage des bougies manquees",
)

MODELE_CHARGE_LE = Gauge(
    "cryptobot_modele_charge_timestamp_seconds",
    "Moment ou le modele actuellement en memoire a ete charge",
)

MODELE_INFO = Gauge(
    "cryptobot_modele_info",
    "Modele en service (la valeur vaut toujours 1, l'information est dans les etiquettes)",
    ["entraine_le", "extrait_sha256", "version"],
)


def signaler_modele(paquet: dict) -> None:
    """Met a jour les metriques decrivant le modele en service."""
    import time

    MODELE_INFO.clear()
    MODELE_INFO.labels(
        entraine_le=str(paquet.get("entraine_le", "inconnu")),
        extrait_sha256=str(paquet.get("extrait_sha256", "inconnu"))[:12],
        version=str(paquet.get("version", "initiale")),
    ).set(1)
    MODELE_CHARGE_LE.set(time.time())
