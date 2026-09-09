"""Optimisation du meilleur modele de chaque profil, par GridSearchCV.

Deux choix qui different d'un GridSearchCV ordinaire :

  1. On optimise le BON SENS DIRECTIONNEL, pas l'accuracy. Optimiser
     l'accuracy ameliorerait la detection des phases calmes, ce qui ne
     rapporte rien a un bot.

  2. La validation croisee utilise TimeSeriesSplit. Le KFold par defaut
     melange passe et futur : le modele s'entrainerait sur des donnees
     posterieures a celles qu'il doit predire, et le score serait
     artificiellement excellent.

Grilles volontairement petites, comme Remy l'a conseille : 3 parametres a
3 valeurs sur 5 decoupages font deja 135 entrainements.

Usage :
    python -m scripts.optimize_model
    python -m scripts.optimize_model --profils day_trading
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GridSearchCV, TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src import config
from src.features import FAMILLES
from src.scoring import SCORER_DIRECTIONNEL, resume
from scripts.compare_models import preparer

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("optimise")

RESULTATS = config.DOCS / "optimisation_modeles.json"

# Le gagnant de chaque profil, d'apres scripts/compare_models.py. Aucun
# modele ne dominant partout, on optimise celui qui convient a chacun.
GAGNANTS = {
    "scalping": (
        LogisticRegression(max_iter=1000),
        {
            "modele__C": [0.01, 0.1, 1.0],
            "modele__class_weight": [None, "balanced"],
        },
    ),
    "day_trading": (
        HistGradientBoostingClassifier(random_state=0),
        {
            "modele__max_iter": [100, 200],
            "modele__learning_rate": [0.05, 0.1],
            "modele__max_leaf_nodes": [15, 31],
        },
    ),
    "swing": (
        RandomForestClassifier(n_jobs=-1, random_state=0),
        {
            "modele__n_estimators": [100, 200],
            "modele__min_samples_leaf": [20, 50, 100],
        },
    ),
}


def optimiser(profil: str) -> dict:
    modele, grille = GAGNANTS[profil]
    X, y = preparer(profil, FAMILLES)

    # Le test reste la periode la plus RECENTE, jamais touchee par la
    # recherche de parametres.
    coupure = int(len(X) * 0.8)
    X_train, X_test = X.iloc[:coupure], X.iloc[coupure:]
    y_train, y_test = y.iloc[:coupure], y.iloc[coupure:]

    pipeline = Pipeline([
        ("imputation", SimpleImputer(strategy="median")),
        ("normalisation", StandardScaler()),
        ("modele", modele),
    ])

    combinaisons = int(np.prod([len(v) for v in grille.values()]))
    log.info("%s : %d combinaisons x 5 decoupages = %d entrainements",
             profil, combinaisons, combinaisons * 5)

    recherche = GridSearchCV(
        pipeline,
        grille,
        scoring=SCORER_DIRECTIONNEL,
        # Decoupages chronologiques emboites : chaque validation porte sur
        # une periode POSTERIEURE a son entrainement.
        cv=TimeSeriesSplit(n_splits=5),
        n_jobs=-1,
        refit=True,
    )

    t0 = time.time()
    recherche.fit(X_train, y_train)
    duree = time.time() - t0

    prediction = recherche.best_estimator_.predict(X_test)
    mesures = resume(y_test, prediction)

    log.info("  meilleurs parametres : %s", recherche.best_params_)
    log.info("  bon sens en validation : %.4f", recherche.best_score_)
    log.info("  bon sens sur le test   : %.4f  (hasard = 0,5000)", mesures["bon_sens"])
    log.info("  taux d'activite        : %.1f %%  sur %d ordres evalues",
             mesures["taux_activite"] * 100, mesures["ordres_evalues"])
    log.info("  accuracy globale       : %.4f", mesures["accuracy_globale"])
    log.info("  duree : %.0f s", duree)

    return {
        "modele": type(modele).__name__,
        "meilleurs_parametres": {k: str(v) for k, v in recherche.best_params_.items()},
        "bon_sens_validation": round(recherche.best_score_, 4),
        "test": mesures,
        "combinaisons": combinaisons,
        "secondes": round(duree),
    }


def main():
    parser = argparse.ArgumentParser(description="Optimisation par GridSearchCV")
    parser.add_argument("--profils", nargs="+", default=list(GAGNANTS))
    args = parser.parse_args()

    resultats = {
        "critere": "bon sens directionnel sur les ordres passes",
        "pourquoi": "L'accuracy globale recompense la detection des phases "
                    "calmes, qui ne rapporte rien a un bot.",
        "validation": "TimeSeriesSplit a 5 decoupages",
        "profils": {},
    }
    for profil in args.profils:
        log.info("=== %s ===", profil)
        resultats["profils"][profil] = optimiser(profil)

    RESULTATS.write_text(json.dumps(resultats, indent=2), encoding="utf-8")
    log.info("Resultats ecrits : %s", RESULTATS)


if __name__ == "__main__":
    main()
