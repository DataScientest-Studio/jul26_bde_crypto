"""L'ordre projete : ou placer le take profit et le stop loss, et ce qui arrivera.

DEUX MODELES REPONDENT A DEUX QUESTIONS DIFFERENTES

    modele de direction   "la prochaine bougie monte-t-elle ?"
                          -> c'est lui qui declenche l'ordre (etape 4)

    modele a barrieres    "si j'ouvre maintenant, vais-je toucher le take
                          profit ou le stop loss en premier ?"
                          -> c'est lui qui dit ce que deviendra l'ordre (etape 3)

Le second a ete entraine sur l'etiquetage a trois barrieres : pour chaque
bougie, on place une cible de gain et une limite de perte a une distance
proportionnelle a la volatilite recente, et on regarde laquelle est touchee
la premiere dans les 12 bougies qui suivent.

OU SONT PLACEES LES BARRIERES
    A `largeur` x l'ecart-type des rendements des 24 dernieres bougies. Une
    distance fixe en pourcentage n'aurait aucun sens : 1 % est enorme sur une
    bougie calme et negligeable un jour de panique.

Le ratio gain/risque vaut donc 1 pour 1 par construction : les deux barrieres
sont a la meme distance. C'est un choix de l'etape 3, pas une contrainte.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from src import config
from src.features import FAMILLES, construire_groupes
from src.labeling import volatilite_glissante
from src.preprocessing import INTERVAL_SECONDS

logger = logging.getLogger(__name__)

CHEMIN_BARRIERES = Path(os.getenv("CRYPTOBOT_MODELE_BARRIERES",
                                  config.ROOT / "models" / "day_trading.joblib"))
FENETRE_VOLATILITE = 24          # identique a l'etiquetage de l'etape 3
NOMS_CLASSES = {-1: "stop loss touche en premier", 0: "aucune barriere touchee",
                1: "take profit touche en premier"}


class ModeleBarrieresIndisponible(RuntimeError):
    """Le modele a barrieres de l'etape 3 est absent."""


@lru_cache(maxsize=1)
def charger_barrieres() -> dict:
    if not CHEMIN_BARRIERES.exists():
        raise ModeleBarrieresIndisponible(
            f"{CHEMIN_BARRIERES} introuvable. Lancer : "
            "python -m scripts.train_final --profils day_trading"
        )
    import joblib

    paquet = joblib.load(CHEMIN_BARRIERES)
    logger.info("Modele a barrieres charge : %s (%.0f sigma, horizon %d)",
                CHEMIN_BARRIERES.name, paquet["largeur_barrieres"], paquet["horizon"])
    return paquet


def projeter(bougies: pd.DataFrame, symbole: str, interval: str, sens: int) -> dict:
    """Niveaux de l'ordre et pronostic du modele a barrieres.

    `sens` vient du modele de direction : +1 achat, -1 vente, 0 aucun ordre.
    Les niveaux sont calcules meme sans ordre, pour que l'interface puisse
    montrer ou ils SERAIENT places.
    """
    paquet = charger_barrieres()
    largeur, horizon = float(paquet["largeur_barrieres"]), int(paquet["horizon"])

    serie = bougies[(bougies["symbol"] == symbole)
                    & (bougies["interval"] == interval)].sort_values("open_time")
    if serie.empty:
        raise ValueError(f"aucune bougie pour {symbole} {interval}")

    volatilite = float(volatilite_glissante(serie["close"], FENETRE_VOLATILITE).iloc[-1])
    if not np.isfinite(volatilite) or volatilite <= 0:
        raise ValueError("volatilite incalculable : pas assez de bougies")

    entree = float(serie["close"].iloc[-1])
    distance = largeur * volatilite                     # en fraction du prix
    # Pour un achat, le gain est au-dessus et la perte en dessous ; pour une
    # vente, l'inverse. Sans ordre, on montre la configuration d'un achat.
    vers_le_haut = sens >= 0
    take_profit = entree * (1 + distance) if vers_le_haut else entree * (1 - distance)
    stop_loss = entree * (1 - distance) if vers_le_haut else entree * (1 + distance)

    derniere = serie["open_time"].iloc[-1]
    duree = pd.Timedelta(seconds=INTERVAL_SECONDS[interval])
    echeance = derniere + duree * (horizon + 1)

    # Pronostic du modele a barrieres. Il utilise les memes variables que
    # l'etape 3 (26 indicateurs + le pas de temps), sans contexte lent.
    variables = construire_groupes(bougies, FAMILLES)
    variables["pas_de_temps"] = np.log(variables["interval"].map(INTERVAL_SECONDS))
    ligne = (variables[(variables["symbol"] == symbole) & (variables["interval"] == interval)]
             .sort_values("open_time").tail(1))
    probabilites = paquet["pipeline"].predict_proba(ligne[paquet["colonnes"]])[0]
    classes = list(paquet["pipeline"].classes_)
    par_classe = {NOMS_CLASSES[int(c)]: round(float(p), 4)
                  for c, p in zip(classes, probabilites)}
    pronostic = int(classes[int(np.argmax(probabilites))])

    # Frais d'un aller-retour, pour que le gain affiche soit celui qu'on
    # touche vraiment.
    frais = 0.002
    return {
        "sens": sens,
        "entree": round(entree, 8),
        "take_profit": round(take_profit, 8),
        "stop_loss": round(stop_loss, 8),
        "distance_pct": round(distance * 100, 3),
        "gain_net_si_take_profit_pct": round((distance - frais) * 100, 3),
        "perte_nette_si_stop_loss_pct": round((-distance - frais) * 100, 3),
        "ratio_gain_risque": 1.0,          # barrieres symetriques, par construction
        "largeur_sigma": largeur,
        "volatilite_recente_pct": round(volatilite * 100, 4),
        "horizon_bougies": horizon,
        "echeance": echeance.isoformat(),
        "pronostic": NOMS_CLASSES[pronostic],
        "pronostic_sens": pronostic,
        "probabilites": par_classe,
        "modele": {"largeur_sigma": largeur, "horizon": horizon,
                   "entraine_le": paquet.get("entraine_le")},
    }
