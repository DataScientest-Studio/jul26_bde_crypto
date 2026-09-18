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
    GET  /ordre/{paire}     l'ordre projete : take profit, stop loss, et ce que
                            le modele a barrieres prevoit qu'il devienne
    POST /positions/{paire} suit le carnet de positions virtuelles : ferme
                            celles dont une barriere est touchee, ouvre si le
                            modele donne un signal, et rend le bilan

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

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from api import derive as module_derive
from api import donnees, modele, ordre as module_ordre
from api import positions as module_positions
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


# L'interface fournie est servie par l'API elle-meme : meme origine, donc pas
# besoin de CORS pour elle. On l'ouvre quand meme pour qu'un front-end separe
# (un projet React sur un autre port, par exemple) puisse appeler l'API sans
# etre bloque par le navigateur. Acceptable ici : l'API n'ecoute qu'en local.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
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


@app.get("/graphique/{symbole}", tags=["interface"], dependencies=[Depends(verifier_cle)])
def graphique(symbole: str, interval: str = Query("15m"), limite: int = Query(120, ge=20, le=500),
              style: str = Query("conservateur"),
              source: str = Query("binance", pattern="^(binance|base)$")) -> dict:
    """Les dernieres bougies AVEC la decision du modele pour chacune.

    C'est le format dont une interface a besoin : un seul appel donne de quoi
    tracer les chandeliers et poser les fleches d'achat et de vente au bon
    endroit.

    `source=binance` lit le marche en direct (la base a toujours du retard
    puisqu'elle n'est alimentee que lorsqu'on lance la collecte) ;
    `source=base` lit les bougies stockees.
    """
    symbole = symbole.upper()
    # Large marge : les indicateurs ont besoin de 100 bougies d'historique
    # avant la premiere que l'on affiche.
    profondeur = limite + modele.BOUGIES_MINIMUM * 2
    try:
        toutes = (donnees.bougies_binance(symbole, profondeur, inclure_en_cours=True)
                  if source == "binance"
                  else donnees.dernieres_bougies(symbole, par_intervalle=profondeur))
    except Exception as exc:
        origine = "Binance" if source == "binance" else "base"
        raise HTTPException(status_code=503, detail=f"{origine} indisponible ({type(exc).__name__})")

    # La bougie en cours est mise de cote : le modele ne predit QUE sur des
    # bougies cloturees, mais l'interface l'affiche pour suivre le prix vivant.
    maintenant = pd.Timestamp.now(tz="UTC")
    en_cours = toutes[(toutes["symbol"] == symbole) & (toutes["interval"] == interval)
                      & (toutes["close_time"] > maintenant)]
    bougies_recentes = toutes[toutes["close_time"] <= maintenant]

    try:
        serie = modele.predire_serie(bougies_recentes, symbole, interval, style, limite)
    except modele.ModeleIndisponible as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    derniere = en_cours.sort_values("open_time").tail(1)
    bougie_en_cours = None
    if not derniere.empty:
        ligne = derniere.iloc[0]
        bougie_en_cours = {
            "open_time": ligne["open_time"].isoformat(),
            "close_time": ligne["close_time"].isoformat(),
            "open": float(ligne["open"]), "high": float(ligne["high"]),
            "low": float(ligne["low"]), "close": float(ligne["close"]),
        }

    return {"symbole": symbole, "interval": interval, "style": style, "source": source,
            "seuils": modele.charger()["styles"][style],
            "bougies": len(serie), "donnees": serie,
            # Affichee, jamais utilisee pour predire.
            "bougie_en_cours": bougie_en_cours,
            "maintenant": maintenant.isoformat()}


@app.get("/ordre/{symbole}", tags=["interface"], dependencies=[Depends(verifier_cle)])
def ordre(symbole: str, interval: str = Query("1h"), style: str = Query("conservateur"),
          source: str = Query("binance", pattern="^(binance|base)$")) -> dict:
    """L'ordre projete a cet instant : niveaux, echeance et pronostic.

    Deux modeles repondent ici. Celui de l'etape 4 decide s'il faut agir ; celui
    de l'etape 3, entraine sur les trois barrieres, dit si le take profit ou le
    stop loss serait touche en premier.
    """
    symbole = symbole.upper()
    profondeur = modele.BOUGIES_MINIMUM * 2
    try:
        bougies_recentes = (donnees.bougies_binance(symbole, profondeur) if source == "binance"
                            else donnees.dernieres_bougies(symbole, par_intervalle=profondeur))
    except Exception as exc:
        origine = "Binance" if source == "binance" else "base"
        raise HTTPException(status_code=503, detail=f"{origine} indisponible ({type(exc).__name__})")

    try:
        decision = modele.predire(bougies_recentes, symbole, interval, style)
        projection = module_ordre.projeter(bougies_recentes, symbole, interval, decision["sens"])
    except (modele.ModeleIndisponible, module_ordre.ModeleBarrieresIndisponible) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {"decision": decision, "ordre": projection}


@app.post("/positions/{symbole}", tags=["interface"], dependencies=[Depends(verifier_cle)])
def positions(symbole: str, interval: str = Query("1h"), style: str = Query("conservateur"),
              source: str = Query("binance", pattern="^(binance|base)$")) -> dict:
    """Fait vivre le carnet de positions virtuelles, puis en rend l'etat.

    Aucun argent reel : on note ce que le bot AURAIT fait. Une seule position
    a la fois par paire, pas de temps et style, comme dans le backtest de
    l'etape 3 - sinon les resultats seraient flattes par des trades qui se
    recouvrent.
    """
    symbole = symbole.upper()
    profondeur = modele.BOUGIES_MINIMUM * 2
    try:
        toutes = (donnees.bougies_binance(symbole, profondeur, inclure_en_cours=True)
                  if source == "binance"
                  else donnees.dernieres_bougies(symbole, par_intervalle=profondeur))
    except Exception as exc:
        origine = "Binance" if source == "binance" else "base"
        raise HTTPException(status_code=503, detail=f"{origine} indisponible ({type(exc).__name__})")

    maintenant = pd.Timestamp.now(tz="UTC")
    cloturees = toutes[toutes["close_time"] <= maintenant]
    serie = toutes[(toutes["symbol"] == symbole) & (toutes["interval"] == interval)]
    prix_actuel = float(serie.sort_values("open_time")["close"].iloc[-1])

    try:
        decision = modele.predire(cloturees, symbole, interval, style)
        projection = module_ordre.projeter(cloturees, symbole, interval, decision["sens"])
        mouvement = module_positions.synchroniser(cloturees, symbole, interval, style,
                                                  decision, projection)
        etat = module_positions.etat(symbole, interval, style, prix_actuel)
    except (modele.ModeleIndisponible, module_ordre.ModeleBarrieresIndisponible) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"carnet indisponible ({type(exc).__name__})")

    return {"symbole": symbole, "interval": interval, "style": style,
            "mouvement": mouvement, **etat}


@app.get("/static/{fichier}", tags=["interface"])
def fichier_statique(fichier: str):
    """Sert la bibliotheque de graphiques, installee localement.

    Elle est livree avec le projet plutot que chargee depuis un site
    exterieur : l'interface fonctionne alors sans acces reseau, et la version
    ne peut pas changer dans notre dos.
    """
    from fastapi.responses import FileResponse

    chemin = (Path(__file__).parent / "static" / fichier).resolve()
    dossier = (Path(__file__).parent / "static").resolve()
    # Sans ce controle, un nom de fichier comme ../../.env sortirait du dossier.
    if dossier not in chemin.parents or not chemin.is_file():
        raise HTTPException(status_code=404, detail="fichier inconnu")
    return FileResponse(chemin, media_type="application/javascript")


@app.get("/interface", response_class=HTMLResponse, tags=["interface"])
def interface() -> HTMLResponse:
    """Page de demonstration, servie par l'API elle-meme.

    Meme origine que les routes qu'elle appelle : rien a configurer, rien a
    lancer en plus. Elle n'est pas protegee par la cle d'API - c'est la page
    qui demande la cle a l'utilisateur si l'API en exige une.
    """
    return HTMLResponse((Path(__file__).parent / "interface.html").read_text(encoding="utf-8"))


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
