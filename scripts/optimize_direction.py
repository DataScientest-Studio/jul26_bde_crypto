"""Optimisation honnete sur la cible "sens de la prochaine bougie".

Le protocole compte plus que le resultat. Le tableau precedent comptait 21
cases ; en choisir la meilleure APRES les avoir vues, c'est optimiser sur le
jeu de test, et c'est exactement ce qu'on reproche aux etudes qui ne se
reproduisent pas.

Trois regles fixees AVANT de lancer quoi que ce soit :

  1. le niveau de selectivite est fige a 5 % - c'est la ou les trois familles
     de modeles s'accordaient, avec un echantillon de test encore consequent
     (4 538 bougies) ;
  2. la recherche d'hyperparametres se fait sur l'ENTRAINEMENT SEUL, en
     validation croisee temporelle (TimeSeriesSplit) ;
  3. le jeu de test n'est touche qu'UNE FOIS, tout a la fin, pour le modele
     retenu. Aucun aller-retour.

La metrique optimisee n'est pas l'accuracy globale mais l'accuracy sur les
5 % de bougies ou le modele est le plus sur : c'est ce que le systeme fera
reellement, donc c'est ce qu'il faut optimiser.

Usage :
    python -m scripts.optimize_direction
    python -m scripts.optimize_direction --profil day_trading
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GridSearchCV, TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src import config
from src.features import colonnes_explicatives
from scripts.direction_prochaine_bougie import FRAIS_ALLER_RETOUR, preparer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("optimisation")

RESULTATS = config.DOCS / "optimisation_direction.json"

# FIGE AVANT L'EXPERIENCE : ne pas toucher apres avoir vu les resultats.
ACTIVITE = 0.05
PLAFOND = 150_000  # lignes les plus recentes, pour tenir le temps de calcul

GRILLES = {
    "foret_aleatoire": (
        RandomForestClassifier(n_jobs=-1, random_state=0),
        {
            "modele__n_estimators": [200, 400],
            "modele__min_samples_leaf": [20, 50, 100],
            "modele__max_features": ["sqrt", 0.5],
        },
    ),
    "gradient_boosting": (
        HistGradientBoostingClassifier(random_state=0),
        {
            "modele__max_iter": [200, 400],
            "modele__learning_rate": [0.05, 0.1],
            "modele__max_leaf_nodes": [15, 31, 63],
        },
    ),
    "regression_logistique": (
        LogisticRegression(max_iter=1000, n_jobs=-1),
        {"modele__C": [0.1, 1.0, 10.0]},
    ),
}


def score_selectif(estimateur, X, y) -> float:
    """Accuracy sur les ACTIVITE % de bougies les plus sures.

    Signature (estimateur, X, y) : c'est ce qu'attend GridSearchCV pour un
    score personnalise. La confiance d'une prediction binaire est la distance
    a 0,5 - une probabilite de 0,52 comme de 0,48 veut dire "je ne sais pas".
    """
    probabilites = estimateur.predict_proba(X)[:, 1]
    prediction = (probabilites >= 0.5).astype(int)
    confiance = np.abs(probabilites - 0.5)
    garde = np.argsort(-confiance)[:max(int(len(probabilites) * ACTIVITE), 1)]
    return float((prediction[garde] == np.asarray(y)[garde]).mean())


def mesure_finale(pipeline, X_test, y_test, rendements) -> dict:
    """L'unique passage sur le jeu de test."""
    probabilites = pipeline.predict_proba(X_test)[:, 1]
    prediction = (probabilites >= 0.5).astype(int)
    confiance = np.abs(probabilites - 0.5)
    garde = np.argsort(-confiance)[:max(int(len(probabilites) * ACTIVITE), 1)]

    juste = prediction[garde] == np.asarray(y_test)[garde]
    sens = np.where(prediction[garde] == 1, 1.0, -1.0)
    brut = np.asarray(rendements)[garde] * sens

    return {
        "bougies": int(len(garde)),
        "accuracy_selective": round(float(juste.mean()), 4),
        "accuracy_globale": round(float((prediction == np.asarray(y_test)).mean()), 4),
        "seuil_confiance": round(float(confiance[garde].min() + 0.5), 4),
        "rendement_moyen_brut_pct": round(float(brut.mean() * 100), 4),
        "rendement_moyen_net_pct": round(float(brut.mean() - FRAIS_ALLER_RETOUR) * 100, 4),
    }


def main():
    parser = argparse.ArgumentParser(description="Optimisation sur le sens de la bougie")
    parser.add_argument("--profil", default="day_trading")
    args = parser.parse_args()

    # Le contexte multi-echelles est retenu : il apportait +1 point dans le
    # regime selectif. Decision prise avant cette experience.
    jeu = preparer(args.profil, contexte=True)
    if len(jeu) > PLAFOND:
        jeu = jeu.tail(PLAFOND).reset_index(drop=True)

    hors_variables = {"rendement_suivant", "label"}
    colonnes = [c for c in colonnes_explicatives(jeu) if c not in hors_variables]
    X, y = jeu[colonnes], jeu["label"]

    coupure = int(len(X) * 0.8)
    X_train, X_test = X.iloc[:coupure], X.iloc[coupure:]
    y_train, y_test = y.iloc[:coupure], y.iloc[coupure:]
    rendements_test = jeu["rendement_suivant"].iloc[coupure:]

    log.info("%s : %d lignes (%d entrainement / %d test), %d variables",
             args.profil, len(X), len(X_train), len(X_test), len(colonnes))
    log.info("Selectivite figee a %.0f %% | validation croisee temporelle 3 plis",
             ACTIVITE * 100)

    # 3 plis : chaque pli entraine sur le passe et valide sur la suite
    # immediate. Jamais l'inverse.
    decoupage = TimeSeriesSplit(n_splits=3)

    resultats = {"activite": ACTIVITE, "lignes": len(X), "variables": len(colonnes),
                 "contexte_multi_echelles": True, "modeles": {}}
    meilleur = None

    for nom, (modele, grille) in GRILLES.items():
        pipeline = Pipeline([
            ("imputation", SimpleImputer(strategy="median")),
            ("normalisation", StandardScaler()),
            ("modele", modele),
        ])
        recherche = GridSearchCV(
            pipeline, grille, scoring=score_selectif, cv=decoupage,
            n_jobs=1, refit=True, verbose=0,
        )
        t0 = time.time()
        recherche.fit(X_train, y_train)
        secondes = round(time.time() - t0, 1)

        log.info("%-24s validation %.4f  (%s combinaisons, %ss)",
                 nom, recherche.best_score_, len(recherche.cv_results_["params"]), secondes)
        for parametre, valeur in recherche.best_params_.items():
            log.info("      %-32s %s", parametre.replace("modele__", ""), valeur)

        resultats["modeles"][nom] = {
            "score_validation": round(float(recherche.best_score_), 4),
            "parametres": {k.replace("modele__", ""): v
                           for k, v in recherche.best_params_.items()},
            "combinaisons": len(recherche.cv_results_["params"]),
            "secondes": secondes,
        }
        if meilleur is None or recherche.best_score_ > meilleur[1]:
            meilleur = (nom, recherche.best_score_, recherche.best_estimator_)

    # --- Le seul passage sur le jeu de test ---
    nom, score_validation, pipeline = meilleur
    log.info("Retenu d'apres la VALIDATION seule : %s (%.4f)", nom, score_validation)

    final = mesure_finale(pipeline, X_test, y_test, rendements_test)
    resultats["retenu"] = {"modele": nom,
                           "score_validation": round(float(score_validation), 4),
                           "test": final}

    log.info("MESURE FINALE sur le test (une seule fois) :")
    log.info("   accuracy selective  %.4f  sur %d bougies (seuil %.3f)",
             final["accuracy_selective"], final["bougies"], final["seuil_confiance"])
    log.info("   accuracy globale    %.4f", final["accuracy_globale"])
    log.info("   rendement net       %+.4f %% par operation", final["rendement_moyen_net_pct"])
    log.info("   objectif 0,60       %s",
             "ATTEINT" if final["accuracy_selective"] >= 0.60 else "NON ATTEINT")

    RESULTATS.write_text(json.dumps(resultats, indent=2), encoding="utf-8")
    log.info("Resultats ecrits : %s", RESULTATS)


if __name__ == "__main__":
    main()
