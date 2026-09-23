"""Qui a le droit d'appeler l'API, et comment on le verifie.

UNE CLE PAR CLIENT, PAS UNE CLE POUR TOUS
    L'API a deux clients connus : l'interface (via le proxy nginx) et Airflow
    (qui fait tourner le bot toutes les 15 minutes). Chacun a SA cle :

        CRYPTOBOT_CLES_API="interface:3f9c...,airflow:b71e..."

    Deux benefices concrets : chaque appel est attribue a un client (journal,
    metriques Prometheus), et une cle compromise se revoque sans couper
    l'autre client.

POURQUOI DES CLES ET PAS DES JETONS JWT
    Le JWT repond a un autre probleme : des PERSONNES qui se connectent avec
    un mot de passe et recoivent un jeton temporaire. Nos clients sont des
    SERVICES, sans utilisateur derriere : une cle longue et aleatoire, tenue
    hors du navigateur, est la reponse standard (c'est ce que font l'API
    Binance, Stripe ou GitHub pour les acces machine a machine).

FERMEE PAR DEFAUT
    Sans cle configuree, les routes protegees repondent 503 : un oubli de
    configuration ne doit jamais ouvrir l'API. L'acces libre existe pour le
    developpement, mais il faut le demander explicitement
    (CRYPTOBOT_ACCES_LIBRE=1).

COMPARAISON EN TEMPS CONSTANT
    `a == b` s'arrete au premier caractere different : en chronometrant les
    refus, un attaquant devinerait la cle caractere par caractere.
    hmac.compare_digest prend toujours le meme temps.
"""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

# Une cle plus courte se devine trop facilement : 32 caracteres hexadecimaux
# = 128 bits d'aleatoire, la taille d'une cle AES.
LONGUEUR_MINIMALE = 32


def lire_cles(valeur: str | None) -> dict[str, str]:
    """"interface:abc,airflow:def" -> {"abc": "interface", "def": "airflow"}.

    Une entree mal formee ou trop courte est ECARTEE avec un avertissement :
    mieux vaut une cle refusee au demarrage qu'une cle faible acceptee.
    """
    cles: dict[str, str] = {}
    for morceau in (valeur or "").split(","):
        morceau = morceau.strip()
        if not morceau:
            continue
        client, separateur, cle = morceau.partition(":")
        client, cle = client.strip(), cle.strip()
        if not separateur or not client:
            logger.warning("Cle d'API ignoree : format attendu client:cle")
            continue
        if len(cle) < LONGUEUR_MINIMALE:
            logger.warning("Cle d'API du client %s ignoree : moins de %d caracteres",
                           client, LONGUEUR_MINIMALE)
            continue
        cles[cle] = client
    return cles


# Lues une fois, au demarrage. Changer une cle = redemarrer le conteneur,
# ce qui est voulu : la configuration de securite ne change pas en douce.
CLES = lire_cles(os.getenv("CRYPTOBOT_CLES_API"))
ACCES_LIBRE = os.getenv("CRYPTOBOT_ACCES_LIBRE", "") == "1"

if ACCES_LIBRE:
    logger.warning("ACCES LIBRE ACTIVE : l'API ne demande aucune cle (developpement seulement)")
elif not CLES:
    logger.warning("Aucune cle d'API configuree : les routes protegees repondront 503")


def identifier(x_api_key: str | None) -> str | None:
    """Le nom du client a qui appartient la cle, ou None.

    Toutes les cles sont comparees, meme apres avoir trouve la bonne : le
    temps de reponse ne dit rien du nombre de cles ni de leur ordre.
    """
    if not x_api_key:
        return None
    trouve = None
    for cle, client in CLES.items():
        if hmac.compare_digest(x_api_key.encode(), cle.encode()):
            trouve = client
    return trouve


def verifier_cle(x_api_key: str | None = Header(default=None)) -> str:
    """Dependance FastAPI : rend le nom du client, ou refuse l'appel."""
    from api import metriques

    if ACCES_LIBRE:
        return "acces-libre"
    if not CLES:
        metriques.APPELS_REFUSES.labels(raison="api_non_configuree").inc()
        raise HTTPException(status_code=503,
                            detail="API non configuree : aucune cle d'API definie")
    client = identifier(x_api_key)
    if client is None:
        raison = "cle_absente" if not x_api_key else "cle_invalide"
        metriques.APPELS_REFUSES.labels(raison=raison).inc()
        # Meme message dans les deux cas : inutile d'indiquer a un attaquant
        # si sa cle existe mais est fausse, ou s'il l'a oubliee.
        raise HTTPException(status_code=401, detail="cle d'API absente ou invalide",
                            headers={"WWW-Authenticate": "ApiKey"})
    metriques.APPELS_PAR_CLIENT.labels(client=client).inc()
    return client
