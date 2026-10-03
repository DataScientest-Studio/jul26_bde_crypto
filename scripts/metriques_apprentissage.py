"""Metriques d'apprentissage : jeu d'entrainement contre jeu de test.

Repond a la question « le modele a-t-il appris quelque chose, ou par coeur ? »
en mesurant les memes indicateurs sur chaque periode du decoupage
chronologique du modele final :

    apprentissage  0-72 %   ce que le modele a vu
    calibration    72-80 %  vu par la calibration seulement
    seuils         80-100 % sert a regler les deux styles
    test           bougies posterieures a l'extrait fige, jamais vues

Le modele final est un CLASSIFIEUR (sens de la prochaine bougie) : RMSE, MAE,
MAPE et R2 ne s'y appliquent pas. On les donne pour la regression de
reference (prix et rendement de la bougie suivante), qui explique pourquoi la
regression a ete abandonnee, et on donne pour le classifieur ses equivalents :
accuracy, ROC-AUC, log loss, score de Brier.

Le modele n'est PAS re-entraine : on recharge models/direction_day_trading.joblib.

Usage :
    python -m scripts.metriques_apprentissage
"""
from __future__ import annotations

import json
import logging
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import (
    accuracy_score, brier_score_loss, log_loss, mean_absolute_error,
    mean_absolute_percentage_error, mean_squared_error, r2_score, roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src import config
from scripts.pistes_amelioration import RECENTES, construire
from scripts.train_direction_final import decider

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("metriques")

RESULTATS = config.DOCS / "metriques_apprentissage.json"


def classification(modele: dict, jeu: pd.DataFrame) -> dict:
    proba = modele["modele"].predict_proba(jeu[modele["colonnes"]])[:, 1]
    y = jeu["label"].to_numpy()
    mesures = {
        "bougies": len(y),
        "accuracy": round(accuracy_score(y, (proba >= 0.5).astype(int)), 4),
        "roc_auc": round(roc_auc_score(y, proba), 4),
        "log_loss": round(log_loss(y, proba), 4),
        "brier": round(brier_score_loss(y, proba), 4),
    }
    sens = decider(proba, modele["styles"]["conservateur"])
    agit = sens != 0
    if agit.any():
        mesures["accuracy_selective_conservateur"] = round(
            float(((sens[agit] > 0) == (y[agit] == 1)).mean()), 4)
        mesures["ordres_conservateur"] = int(agit.sum())
    return mesures


def regression(cible: str) -> dict:
    """Regression de reference sur le profil day trading, train ET test."""
    from scripts.regression_baseline import preparer

    X, y, naif = preparer("day_trading", cible)
    coupure = int(len(X) * 0.8)

    def mesurer(vrai, prediction):
        sortie = {
            "r2": round(r2_score(vrai, prediction), 6),
            "rmse": round(float(np.sqrt(mean_squared_error(vrai, prediction))), 6),
            "mae": round(mean_absolute_error(vrai, prediction), 6),
        }
        # La MAPE divise par la valeur vraie : elle n'a de sens que pour le
        # prix. Un rendement vaut souvent presque zero, la MAPE explose.
        if cible == "prix":
            sortie["mape_pct"] = round(mean_absolute_percentage_error(vrai, prediction) * 100, 4)
        return sortie

    resultats = {}
    for nom, modele in (("ridge", Ridge(alpha=1.0)),
                        ("gradient_boosting", HistGradientBoostingRegressor(random_state=0))):
        pipeline = Pipeline([("imputation", SimpleImputer(strategy="median")),
                             ("normalisation", StandardScaler()), ("modele", modele)])
        pipeline.fit(X.iloc[:coupure], y.iloc[:coupure])
        resultats[nom] = {
            "entrainement": mesurer(y.iloc[:coupure], pipeline.predict(X.iloc[:coupure])),
            "test": mesurer(y.iloc[coupure:], pipeline.predict(X.iloc[coupure:])),
        }
    resultats["modele_naif"] = {
        "entrainement": mesurer(y.iloc[:coupure], naif.iloc[:coupure]),
        "test": mesurer(y.iloc[coupure:], naif.iloc[coupure:]),
    }
    return resultats


def main():
    modele = joblib.load(config.ROOT / "models" / "direction_day_trading.joblib")
    extrait = pd.read_parquet(config.ROOT / "data" / "extract" / "day_trading.parquet")
    jeu = construire(extrait, set())
    n = len(jeu)
    i72, i80 = int(n * 0.72), int(n * 0.80)
    recentes = construire(pd.read_parquet(RECENTES), set())
    recentes = recentes[recentes["open_time"] > extrait["open_time"].max()].reset_index(drop=True)

    periodes = {"apprentissage": jeu.iloc[:i72], "calibration": jeu.iloc[i72:i80],
                "seuils": jeu.iloc[i80:], "test_jamais_vu": recentes}
    sortie = {"classifieur_final": {}}
    for nom, partie in periodes.items():
        sortie["classifieur_final"][nom] = classification(modele, partie)
        log.info("%-15s %s", nom, sortie["classifieur_final"][nom])

    sortie["regression_reference_day_trading"] = {c: regression(c) for c in ("prix", "rendement")}
    for cible, res in sortie["regression_reference_day_trading"].items():
        for nom, r in res.items():
            log.info("%-9s %-18s train %s | test %s", cible, nom, r["entrainement"], r["test"])

    RESULTATS.write_text(json.dumps(sortie, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Ecrit : %s", RESULTATS)


if __name__ == "__main__":
    main()
