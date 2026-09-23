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

    GET  /metrics           metriques Prometheus (reseau Docker interne seulement)

SECURITE (detail dans api/securite.py)
    - toutes les routes sauf /, /health et /docs exigent l'en-tete X-API-Key ;
    - une cle par client (interface, airflow), comparee en temps constant ;
    - fermee par defaut : sans cle configuree, les routes protegees repondent
      503 plutot que de s'ouvrir ;
    - paires, pas de temps et styles valides par liste blanche AVANT tout
      appel a Binance ou a la base ;
    - l'API n'est pas publiee sur la machine : seul le proxy nginx de
      l'interface (qui injecte la cle) et Airflow la joignent.

L'interface graphique n'est plus servie ici : elle vit dans son propre
conteneur (interface/), derriere nginx.

Lancement en developpement, sans cle :
    CRYPTOBOT_ACCES_LIBRE=1 uvicorn api.main:app --reload
    http://localhost:8000/docs  (documentation interactive, generee seule)
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from api import derive as module_derive
from api import donnees, metriques, modele, ordre as module_ordre
from api import positions as module_positions
from api.schemas import INTERVALLES, STYLES, DemandeDePrediction, Derive, Prediction, Sante
from api.securite import verifier_cle
from src import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("api")

VERSION = "1.1.0"

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
    # Derriere nginx, l'API est publiee sous /api : la documentation /docs
    # doit alors chercher /api/openapi.json. Les appels directs (Airflow,
    # Prometheus, tests) continuent de fonctionner sans le prefixe.
    root_path=os.getenv("CRYPTOBOT_PREFIXE", ""),
)


# Aucune origine autorisee par defaut : l'interface passe par le meme nginx
# que l'API, elle n'a donc pas besoin de CORS. Un front-end sur un autre
# domaine devra etre declare explicitement, jamais "*".
_ORIGINES = [o.strip() for o in os.getenv("CRYPTOBOT_ORIGINES", "").split(",") if o.strip()]
if _ORIGINES:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ORIGINES,
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Content-Type"],
    )


# Metriques HTTP generiques (appels, durees, erreurs par route) pour
# Prometheus. La route /metrics n'est pas documentee : elle n'est pas faite
# pour les humains, et nginx ne la transmet pas.
try:
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator(excluded_handlers=["/metrics", "/health"]).instrument(app).expose(
        app, endpoint="/metrics", include_in_schema=False)
except ImportError:                                    # pragma: no cover
    logger.warning("prometheus-fastapi-instrumentator absent : pas de /metrics")


def paire_connue(symbole: str) -> str:
    """Liste blanche : une paire inconnue ne declenche aucun appel a Binance.

    Sans ce controle, n'importe quelle chaine partait dans une URL Binance et
    consommait notre quota de requetes.
    """
    symbole = symbole.upper()
    if symbole not in config.PAIRS:
        raise HTTPException(status_code=404,
                            detail=f"paire inconnue : {symbole} (disponibles : {', '.join(config.PAIRS)})")
    return symbole


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
def bougies(symbole: str = Depends(paire_connue), interval: INTERVALLES = Query("1h"),
            limite: int = Query(100, ge=1, le=1000)) -> dict:
    try:
        toutes = donnees.dernieres_bougies(symbole, par_intervalle=limite)
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
    symbole = paire_connue(demande.symbole)
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
    metriques.DECISIONS.labels(demande.interval, demande.style, resultat["decision"]).inc()
    metriques.PROBABILITE_HAUSSE.observe(resultat["probabilite_hausse"])
    return Prediction(**resultat)


@app.get("/graphique/{symbole}", tags=["interface"], dependencies=[Depends(verifier_cle)])
def graphique(symbole: str = Depends(paire_connue), interval: INTERVALLES = Query("15m"),
              limite: int = Query(120, ge=20, le=3000), style: STYLES = Query("conservateur"),
              source: str = Query("binance", pattern="^(binance|base)$")) -> dict:
    """Les dernieres bougies AVEC la decision du modele pour chacune.

    C'est le format dont une interface a besoin : un seul appel donne de quoi
    tracer les chandeliers et poser les fleches d'achat et de vente au bon
    endroit.

    `source=binance` lit le marche en direct (la base a toujours du retard
    puisqu'elle n'est alimentee que lorsqu'on lance la collecte) ;
    `source=base` lit les bougies stockees.
    """
    # Large marge : les indicateurs ont besoin de 100 bougies d'historique
    # avant la premiere que l'on affiche.
    profondeur = limite + modele.BOUGIES_MINIMUM * 2
    try:
        toutes = (donnees.bougies_binance(symbole, profondeur, inclure_en_cours=True)
                  if source == "binance"
                  else donnees.dernieres_bougies(symbole, par_intervalle=profondeur))
    except Exception as exc:
        origine = "Binance" if source == "binance" else "base"
        metriques.ERREURS_SOURCES.labels(source=origine.lower()).inc()
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
def ordre(symbole: str = Depends(paire_connue), interval: INTERVALLES = Query("1h"),
          style: STYLES = Query("conservateur"),
          source: str = Query("binance", pattern="^(binance|base)$")) -> dict:
    """L'ordre projete a cet instant : niveaux, echeance et pronostic.

    Deux modeles repondent ici. Celui de l'etape 4 decide s'il faut agir ; celui
    de l'etape 3, entraine sur les trois barrieres, dit si le take profit ou le
    stop loss serait touche en premier.
    """
    profondeur = modele.BOUGIES_MINIMUM * 2
    try:
        bougies_recentes = (donnees.bougies_binance(symbole, profondeur) if source == "binance"
                            else donnees.dernieres_bougies(symbole, par_intervalle=profondeur))
    except Exception as exc:
        origine = "Binance" if source == "binance" else "base"
        metriques.ERREURS_SOURCES.labels(source=origine.lower()).inc()
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
def positions(symbole: str = Depends(paire_connue), interval: INTERVALLES = Query("1h"),
              style: STYLES = Query("conservateur"),
              source: str = Query("binance", pattern="^(binance|base)$")) -> dict:
    """Fait vivre le carnet de positions virtuelles, puis en rend l'etat.

    Aucun argent reel : on note ce que le bot AURAIT fait. Une seule position
    a la fois par paire, pas de temps et style, comme dans le backtest de
    l'etape 3 - sinon les resultats seraient flattes par des trades qui se
    recouvrent.
    """

    # Bougies manquees pendant que personne ne regardait (page fermee, Docker
    # arrete) : on les rejoue AVANT le suivi normal, sinon le carnet aurait
    # des trous. Au mieux : un echec ici ne doit pas empecher d'afficher le
    # carnet tel qu'il est.
    rattrapage = None
    try:
        manquantes = module_positions.bougies_a_rattraper(symbole, interval, style)
        if manquantes:
            barrieres = module_ordre.charger_barrieres()
            profondeur_rattrapage = manquantes + modele.BOUGIES_MINIMUM + 30
            historique = (donnees.bougies_binance(symbole, profondeur_rattrapage)
                          if source == "binance"
                          else donnees.dernieres_bougies(symbole, par_intervalle=profondeur_rattrapage))
            rattrapage = module_positions.rattraper(
                historique, symbole, interval, style,
                float(barrieres["largeur_barrieres"]), int(barrieres["horizon"]))
            metriques.POSITIONS_RATTRAPEES.inc(rattrapage["positions_ajoutees"])
    except Exception as exc:
        logger.warning("Rattrapage du carnet impossible (%s) : %s", type(exc).__name__, exc)

    profondeur = modele.BOUGIES_MINIMUM * 2
    try:
        toutes = (donnees.bougies_binance(symbole, profondeur, inclure_en_cours=True)
                  if source == "binance"
                  else donnees.dernieres_bougies(symbole, par_intervalle=profondeur))
    except Exception as exc:
        origine = "Binance" if source == "binance" else "base"
        metriques.ERREURS_SOURCES.labels(source=origine.lower()).inc()
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
        metriques.DECISIONS.labels(interval, style, decision["decision"]).inc()
        metriques.PROBABILITE_HAUSSE.observe(decision["probabilite_hausse"])
        etat = module_positions.etat(symbole, interval, style, prix_actuel)
    except (modele.ModeleIndisponible, module_ordre.ModeleBarrieresIndisponible) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"carnet indisponible ({type(exc).__name__})")

    return {"symbole": symbole, "interval": interval, "style": style,
            "mouvement": mouvement, "rattrapage": rattrapage, **etat}


@app.get("/derive", response_model=Derive, tags=["surveillance"],
         dependencies=[Depends(verifier_cle)])
def derive(symbole: str = Query("BTCUSDT"), interval: INTERVALLES = Query("1h"),
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

    symbole = paire_connue(symbole)
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
