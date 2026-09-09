"""Backtest des modeles optimises : combien auraient-ils rapporte ?

C'est la mesure qui compte reellement pour un bot. L'accuracy et le bon
sens directionnel sont des indicateurs intermediaires ; seul le capital
final dit si la strategie tient.

Chaque profil est simule deux fois :
  - avec son modele optimise
  - avec un modele PARFAIT, qui connaitrait l'avenir

Le second est le plafond de la strategie. Il repond a une question qu'on
ne peut pas se poser autrement : si meme la connaissance parfaite du futur
ne rapporte rien, le probleme n'est pas le modele mais les frais ou
l'etiquetage.

Usage :
    python -m scripts.backtest_models
    python -m scripts.backtest_models --mise 0.25
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src import config
from src.backtest import simuler, simuler_parfait
from src.features import FAMILLES, colonnes_explicatives, construire_groupes
from src.labeling import etiqueter_groupes
from src.preprocessing import INTERVAL_SECONDS
from src.scoring import resume

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest")

RESULTATS = config.DOCS / "backtest.json"
PLAFOND = 150_000

# Modeles retenus apres comparaison, avec les parametres issus de
# l'optimisation par GridSearchCV sur le bon sens directionnel.
MODELES = {
    "scalping": LogisticRegression(max_iter=1000, C=1.0),
    "day_trading": HistGradientBoostingClassifier(
        random_state=0, max_iter=100, max_leaf_nodes=31, learning_rate=0.1
    ),
    "swing": RandomForestClassifier(
        n_jobs=-1, random_state=0, n_estimators=100, min_samples_leaf=20
    ),
}


def preparer_avec_sorties(profil: str) -> pd.DataFrame:
    """Charge le profil avec ses variables, ses etiquettes ET ses sorties.

    Le backtest a besoin de deux colonnes que l'entrainement n'utilise pas :
    le rendement obtenu a la sortie et le nombre de bougies avant celle-ci.
    Elles doivent etre exclues des variables explicatives - elles decrivent
    le futur, les donner au modele serait la fuite la plus grossiere.
    """
    brut = pd.read_parquet(config.ROOT / "data" / "extract" / f"{profil}.parquet")
    etiquettes = etiqueter_groupes(brut, largeur=3.0, horizon=12)
    variables = construire_groupes(brut, FAMILLES)

    cle = ["symbol", "interval", "open_time"]
    jeu = variables.merge(
        etiquettes[cle + ["label", "rendement_a_la_sortie", "bougies_avant_sortie"]],
        on=cle, how="inner",
    )
    jeu = jeu.dropna(subset=["label"]).sort_values("open_time").reset_index(drop=True)
    if len(jeu) > PLAFOND:
        jeu = jeu.tail(PLAFOND).reset_index(drop=True)
    jeu["pas_de_temps"] = np.log(jeu["interval"].map(INTERVAL_SECONDS))
    return jeu


def colonnes_modele(jeu: pd.DataFrame) -> list[str]:
    """Variables donnees au modele : ni les identifiants, ni les sorties."""
    interdites = {"rendement_a_la_sortie", "bougies_avant_sortie"}
    return [c for c in colonnes_explicatives(jeu) if c not in interdites]


def main():
    parser = argparse.ArgumentParser(description="Backtest des modeles")
    parser.add_argument("--capital", type=float, default=10_000.0)
    parser.add_argument("--mise", type=float, default=0.10,
                        help="Fraction du capital engagee par operation")
    args = parser.parse_args()

    resultats = {
        "capital_initial": args.capital,
        "mise_par_operation": args.mise,
        "frais_par_ordre_pct": 0.1,
        "note": "Mise fixe plutot que reinvestissement total : engager 100 % "
                "du capital a chaque fois fait exploser ou aneantir le "
                "resultat par les seuls interets composes.",
        "profils": {},
    }

    for profil, modele in MODELES.items():
        log.info("=== %s ===", profil)
        jeu = preparer_avec_sorties(profil)
        colonnes = colonnes_modele(jeu)
        X, y = jeu[colonnes], jeu["label"].astype(int)

        coupure = int(len(X) * 0.8)
        pipeline = Pipeline([
            ("imputation", SimpleImputer(strategy="median")),
            ("normalisation", StandardScaler()),
            ("modele", modele),
        ])
        pipeline.fit(X.iloc[:coupure], y.iloc[:coupure])
        prediction = pipeline.predict(X.iloc[coupure:])
        test = jeu.iloc[coupure:].reset_index(drop=True)

        mesures = resume(y.iloc[coupure:], prediction)
        reel = simuler(prediction, test["rendement_a_la_sortie"],
                       test["bougies_avant_sortie"],
                       capital_initial=args.capital, fraction_engagee=args.mise)
        parfait = simuler_parfait(test["rendement_a_la_sortie"],
                                  test["bougies_avant_sortie"], test["label"],
                                  capital_initial=args.capital,
                                  fraction_engagee=args.mise)

        log.info("  bon sens %.4f | %d operations", mesures["bon_sens"], reel["operations"])
        log.info("  gain net moyen  %+.4f %% par operation", reel["gain_moyen_net_pct"])
        log.info("  capital final   %,.0f EUR  (%+.1f %%)".replace(",", ""),
                 reel["capital_final"], reel["performance_pct"])
        log.info("  modele parfait  %+.4f %% par operation, %.1f %% de reussite",
                 parfait["gain_moyen_net_pct"], parfait["taux_reussite_pct"])

        resultats["profils"][profil] = {
            "modele": type(modele).__name__,
            "mesures_modele": mesures,
            "backtest": reel,
            "plafond_theorique": parfait,
        }

    RESULTATS.write_text(json.dumps(resultats, indent=2), encoding="utf-8")
    log.info("Resultats ecrits : %s", RESULTATS)


if __name__ == "__main__":
    main()
