"""Chargement du modele et calcul d'une prediction.

Le modele vient de models/direction_day_trading.joblib, produit par
scripts/train_direction_final.py. Le fichier contient tout ce qu'il faut pour
predire sans rien recalculer ailleurs : le modele calibre, la liste EXACTE des
variables dans le bon ordre, et les deux seuils du bouton.

Il est charge UNE SEULE FOIS au demarrage : le fichier pese une centaine de
mega-octets, le recharger a chaque appel rendrait l'API inutilisable.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

import pandas as pd

from src import config
from src.features import FAMILLES, ajouter_contexte_lent, construire_groupes
from src.preprocessing import INTERVAL_SECONDS

logger = logging.getLogger(__name__)

CHEMIN_MODELE = Path(os.getenv("CRYPTOBOT_MODELE",
                               config.ROOT / "models" / "direction_day_trading.joblib"))

# Bougies necessaires au calcul des variables : la plus longue fenetre est la
# moyenne mobile 100, a laquelle on ajoute une marge.
BOUGIES_MINIMUM = 150


class ModeleIndisponible(RuntimeError):
    """Le fichier du modele est absent : l'API repond mais ne predit pas."""


@lru_cache(maxsize=1)
def charger() -> dict:
    """Charge le modele une fois pour toutes."""
    if not CHEMIN_MODELE.exists():
        raise ModeleIndisponible(
            f"{CHEMIN_MODELE} introuvable. Lancer d'abord : "
            "python -m scripts.train_direction_final"
        )
    import joblib

    paquet = joblib.load(CHEMIN_MODELE)
    logger.info("Modele charge : %s (%d variables, seuils %s)",
                CHEMIN_MODELE.name, len(paquet["colonnes"]), paquet["styles"])
    return paquet


def metadonnees() -> dict:
    """Ce que l'API expose sur le modele, sans le modele lui-meme."""
    paquet = charger()
    return {
        "cible": paquet["cible"],
        "profil": paquet["profil"],
        "variables": len(paquet["colonnes"]),
        "styles": paquet["styles"],
        "style_le_plus_prudent": paquet.get("style_le_plus_prudent", "conservateur"),
        "entraine_le": paquet["entraine_le"],
        "extrait_sha256": paquet["extrait_sha256"],
        "accuracy_globale": paquet.get("accuracy_globale"),
        "mesures": paquet.get("mesures", {}),
    }


def predire_serie(bougies: pd.DataFrame, symbole: str, interval: str, style: str,
                  limite: int = 200) -> list[dict]:
    """Une prediction PAR BOUGIE, pour tracer un graphique.

    Le graphique a besoin de la decision a chaque instant, pas seulement a la
    derniere bougie : c'est ce qui permet d'afficher les fleches d'achat et de
    vente au bon endroit. Un seul calcul de variables sert toute la serie.
    """
    import numpy as np

    paquet = charger()
    if style not in paquet["styles"]:
        raise ValueError(f"style inconnu : {style} (attendu : {list(paquet['styles'])})")

    variables = construire_groupes(bougies, FAMILLES)
    variables = ajouter_contexte_lent(variables)
    variables["pas_de_temps"] = np.log(variables["interval"].map(INTERVAL_SECONDS))

    lignes = (variables[(variables["symbol"] == symbole) & (variables["interval"] == interval)]
              .sort_values("open_time").tail(limite))
    if lignes.empty:
        raise ValueError(f"aucune bougie exploitable pour {symbole} {interval}")

    probabilites = paquet["modele"].predict_proba(lignes[paquet["colonnes"]])[:, 1]
    # Un seuil par cote : les probabilites du modele ne sont pas symetriques,
    # et un seuil unique ne produisait que des ventes.
    seuils = paquet["styles"][style]
    decisions = np.where(probabilites >= seuils["achat"], "acheter",
                         np.where(probabilites <= seuils["vente"], "vendre", "attendre"))

    bougies_tracees = (bougies[(bougies["symbol"] == symbole) & (bougies["interval"] == interval)]
                       .sort_values("open_time").set_index("open_time"))

    serie = []
    for horodatage, probabilite, decision in zip(lignes["open_time"], probabilites, decisions):
        if horodatage not in bougies_tracees.index:
            continue
        bougie = bougies_tracees.loc[horodatage]
        serie.append({
            "open_time": horodatage.isoformat(),
            "open": float(bougie["open"]), "high": float(bougie["high"]),
            "low": float(bougie["low"]), "close": float(bougie["close"]),
            "volume": float(bougie["volume"]),
            "probabilite_hausse": round(float(probabilite), 4),
            "decision": str(decision),
        })
    return serie


def predire(bougies: pd.DataFrame, symbole: str, interval: str, style: str) -> dict:
    """Decision pour la DERNIERE bougie cloturee de ce couple paire/pas de temps.

    Le style choisit le seuil : sous ce seuil, le modele s'abstient. C'est le
    bouton conservateur / agressif, et c'est ce qui fait la difference entre
    50 ordres par jour et 13.
    """
    import numpy as np

    paquet = charger()
    if style not in paquet["styles"]:
        raise ValueError(f"style inconnu : {style} (attendu : {list(paquet['styles'])})")

    variables = construire_groupes(bougies, FAMILLES)
    variables = ajouter_contexte_lent(variables)
    variables["pas_de_temps"] = np.log(variables["interval"].map(INTERVAL_SECONDS))

    ligne = (variables[(variables["symbol"] == symbole) & (variables["interval"] == interval)]
             .sort_values("open_time").tail(1))
    if ligne.empty:
        raise ValueError(f"aucune bougie exploitable pour {symbole} {interval}")

    manquantes = [c for c in paquet["colonnes"] if c not in ligne.columns]
    if manquantes:
        raise ValueError(f"variables manquantes : {manquantes[:5]}")

    probabilite = float(paquet["modele"].predict_proba(ligne[paquet["colonnes"]])[0, 1])
    seuils = paquet["styles"][style]

    if probabilite >= seuils["achat"]:
        decision, sens = "acheter", 1
    elif probabilite <= seuils["vente"]:
        decision, sens = "vendre", -1
    else:
        decision, sens = "attendre", 0

    return {
        "symbole": symbole,
        "interval": interval,
        "style": style,
        "bougie": ligne["open_time"].iloc[0].isoformat(),
        "probabilite_hausse": round(probabilite, 4),
        "seuil_achat": seuils["achat"],
        "seuil_vente": seuils["vente"],
        "decision": decision,
        "sens": sens,
        # Rappel honnete : ce modele a un avantage reel mais faible, et il
        # n'est pas rentable une fois les frais deduits.
        "avertissement": "modele experimental, non rentable frais compris",
    }
