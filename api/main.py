"""API du CryptoBot : donnees, prediction et derive.

    GET  /                  ce que fait l'API
    GET  /health            l'API, le modele et les deux bases repondent-ils ?
    GET  /modele            d'ou vient le modele, et ce qu'il vaut
    GET  /paires            les paires disponibles en base
    GET  /bougies/{paire}   les dernieres bougies (lecture de la base)
    GET  /couverture        volume et retard de collecte par jeu de donnees
    POST /prediction        acheter / vendre / attendre, selon le style
    GET  /derive            les donnees recentes (fenetre en jours) ressemblent-elles
                            a celles de l'entrainement ?

SECURITE
    Si la variable d'environnement CRYPTOBOT_API_KEY est definie, toutes les
    routes sauf / et /health exigent l'en-tete `X-API-Key`. Sans cette
    variable, l'API reste ouverte : pratique en developpement, a ne pas faire
    sur un serveur accessible.

Lancement :
    uvicorn api.main:app --reload
    http://localhost:8000/docs  (documentation interactive, generee seule)
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from api import derive as module_derive
from api import donnees, modele
from api.schemas import DemandeDePrediction, Derive, Prediction, Sante

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("api")

VERSION = "1.0.0"
# `or None` : docker-compose transmet une variable VIDE quand elle n'est pas
# definie. Sans cela, l'API exigerait une cle egale a "" et refuserait tout.
CLE_ATTENDUE = os.getenv("CRYPTOBOT_API_KEY") or None

app = FastAPI(
    title="CryptoBot",
    version=VERSION,
    description=(
        "Predit le sens de la prochaine bougie sur cinq paires Binance, avec "
        "deux styles : agressif (beaucoup d'ordres) ou conservateur (peu "
        "d'ordres, plus surs). Le modele a un avantage reel mais faible "
        "(environ 57 % de bonnes reponses) et n'est PAS rentable une fois les "
        "frais deduits : il est fourni a titre pedagogique."
    ),
)


def verifier_cle(x_api_key: str | None = Header(default=None)) -> None:
    """Controle la cle d'API quand une cle est configuree."""
    if CLE_ATTENDUE is None:
        return
    if x_api_key != CLE_ATTENDUE:
        raise HTTPException(status_code=401, detail="cle d'API absente ou invalide")


@app.get("/", tags=["general"])
def racine() -> dict:
    return {
        "nom": "CryptoBot",
        "version": VERSION,
        "documentation": "/docs",
        "cible": "sens de la prochaine bougie (profil day trading)",
        "avertissement": "modele experimental, non rentable frais compris",
    }


@app.get("/health", response_model=Sante, tags=["general"])
def sante() -> Sante:
    """Sert aussi de sonde a Docker : l'API est-elle vraiment prete ?"""
    try:
        modele.charger()
        etat_modele = "charge"
    except modele.ModeleIndisponible:
        etat_modele = "absent"
    bases = donnees.etat_bases()
    statut = "ok" if etat_modele == "charge" and all(v == "ok" for v in bases.values()) else "degrade"
    return Sante(statut=statut, modele=etat_modele, bases=bases, version=VERSION)


@app.get("/modele", tags=["modele"], dependencies=[Depends(verifier_cle)])
def informations_modele() -> dict:
    try:
        return modele.metadonnees()
    except modele.ModeleIndisponible as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/paires", tags=["donnees"], dependencies=[Depends(verifier_cle)])
def paires() -> dict:
    try:
        return {"paires": donnees.paires_disponibles()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"base indisponible ({type(exc).__name__})")


@app.get("/bougies/{symbole}", tags=["donnees"], dependencies=[Depends(verifier_cle)])
def bougies(symbole: str, interval: str = Query("1h"),
            limite: int = Query(100, ge=1, le=1000)) -> dict:
    try:
        toutes = donnees.dernieres_bougies(symbole.upper(), par_intervalle=limite)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"base indisponible ({type(exc).__name__})")

    lignes = toutes[toutes["interval"] == interval].tail(limite)
    if lignes.empty:
        raise HTTPException(status_code=404,
                            detail=f"aucune bougie pour {symbole} en {interval}")
    return {"symbole": symbole.upper(), "interval": interval, "bougies": len(lignes),
            "donnees": lignes.assign(
                open_time=lambda d: d["open_time"].astype(str),
                close_time=lambda d: d["close_time"].astype(str)).to_dict("records")}


@app.get("/couverture", tags=["donnees"], dependencies=[Depends(verifier_cle)])
def couverture() -> dict:
    try:
        return {"jeux_de_donnees": donnees.couverture()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"base indisponible ({type(exc).__name__})")


@app.post("/prediction", response_model=Prediction, tags=["modele"],
          dependencies=[Depends(verifier_cle)])
def prediction(demande: DemandeDePrediction) -> Prediction:
    """Predit le sens de la prochaine bougie a partir des donnees EN BASE."""
    symbole = demande.symbole.upper()
    try:
        bougies_recentes = donnees.dernieres_bougies(symbole,
                                                     par_intervalle=modele.BOUGIES_MINIMUM * 2)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"base indisponible ({type(exc).__name__})")

    if bougies_recentes.empty:
        raise HTTPException(status_code=404, detail=f"aucune donnee pour {symbole}")

    try:
        resultat = modele.predire(bougies_recentes, symbole, demande.interval, demande.style)
    except modele.ModeleIndisponible as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    donnees.journaliser_prediction(resultat)
    return Prediction(**resultat)


@app.get("/derive", response_model=Derive, tags=["surveillance"],
         dependencies=[Depends(verifier_cle)])
def derive(symbole: str = Query("BTCUSDT"), interval: str = Query("1h"),
           jours: int = Query(90, ge=7, le=365)) -> Derive:
    """Les donnees recentes ressemblent-elles a celles de l'entrainement ?

    La fenetre se donne en JOURS, pas en nombre de bougies. Une fenetre trop
    courte fait sortir en "derive forte" les variables lentes (moyenne mobile
    100, contexte 4h) simplement parce que quelques jours ne couvrent qu'une
    petite partie de leur distribution habituelle. Trois mois est un bon
    compromis : assez long pour que ces variables respirent, assez court pour
    reperer un changement de regime.
    """
    from src.preprocessing import INTERVAL_SECONDS
    from src.features import FAMILLES, ajouter_contexte_lent, construire_groupes

    bougies = min(int(jours * 86400 / INTERVAL_SECONDS[interval]), 15_000)
    try:
        recentes = donnees.dernieres_bougies(symbole.upper(), par_intervalle=bougies)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"base indisponible ({type(exc).__name__})")
    if recentes.empty:
        raise HTTPException(status_code=404, detail=f"aucune donnee pour {symbole}")

    variables = ajouter_contexte_lent(construire_groupes(recentes, FAMILLES))
    try:
        return Derive(**module_derive.mesurer(variables, symbole=symbole.upper(),
                                              interval=interval))
    except module_derive.ReferenceIndisponible as exc:
        raise HTTPException(status_code=503, detail=str(exc))
