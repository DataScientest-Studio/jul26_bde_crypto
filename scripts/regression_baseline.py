"""Volet regression : R2, RMSE et MAE sur la prediction du rendement.

Ce volet n'est pas la strategie retenue - le modele de decision est un
classifieur. Il repond a deux besoins :

  1. Fournir les metriques que Remy a nommees (R2 notamment), qui ne
     s'appliquent pas a un classifieur.
  2. Documenter le piege qui nous a fait abandonner la regression, avec
     nos propres chiffres.

Le piege : predire le PRIX donne un R2 superbe et sans valeur, parce que
le meilleur predicteur du prix de demain est le prix d'aujourd'hui. On
mesure donc systematiquement le modele NAIF a cote - "demain = aujourd'hui"
- pour montrer ce que le modele apporte reellement.

Usage :
    python -m scripts.regression_baseline
"""
from __future__ import annotations

import json
import logging
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mlflow
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src import config
from src.features import FAMILLES, colonnes_explicatives, construire_groupes

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("regression")

SUIVI = f"sqlite:///{(config.ROOT / 'mlflow.db').as_posix()}"


def preparer(profil: str, cible: str) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Prepare le jeu pour predire soit le PRIX suivant, soit le RENDEMENT.

    Retourne aussi la prediction naive, indispensable pour interpreter le R2.
    """
    brut = pd.read_parquet(config.ROOT / "data" / "extract" / f"{profil}.parquet")
    variables = construire_groupes(brut, FAMILLES)

    cle = ["symbol", "interval", "open_time"]
    jeu = variables.merge(brut[cle + ["close"]], on=cle, how="inner")
    jeu = jeu.sort_values(["symbol", "interval", "open_time"]).reset_index(drop=True)

    groupes = jeu.groupby(["symbol", "interval"], sort=False)["close"]
    if cible == "prix":
        y = groupes.shift(-1)
        # Le predicteur le plus bete possible : "demain = aujourd'hui".
        naif = jeu["close"]
    else:
        y = groupes.shift(-1) / jeu["close"] - 1.0
        # Pour un rendement, le naif est "aucun changement".
        naif = pd.Series(0.0, index=jeu.index)

    jeu["_y"], jeu["_naif"] = y, naif
    jeu = jeu.dropna(subset=["_y"]).sort_values("open_time").reset_index(drop=True)
    if len(jeu) > 150_000:
        jeu = jeu.tail(150_000).reset_index(drop=True)

    colonnes = [c for c in colonnes_explicatives(jeu)
                if c not in ("close", "_y", "_naif")]
    return jeu[colonnes], jeu["_y"], jeu["_naif"]


def evaluer(profil: str, cible: str) -> dict:
    X, y, naif = preparer(profil, cible)
    coupure = int(len(X) * 0.8)
    X_test, y_test, naif_test = X.iloc[coupure:], y.iloc[coupure:], naif.iloc[coupure:]

    resultats = {}
    for nom, modele in (("ridge", Ridge(alpha=1.0)),
                        ("gradient_boosting", HistGradientBoostingRegressor(random_state=0))):
        pipeline = Pipeline([
            ("imputation", SimpleImputer(strategy="median")),
            ("normalisation", StandardScaler()),
            ("modele", modele),
        ])
        pipeline.fit(X.iloc[:coupure], y.iloc[:coupure])
        prediction = pipeline.predict(X_test)
        resultats[nom] = {
            "r2": round(r2_score(y_test, prediction), 6),
            "rmse": round(float(np.sqrt(mean_squared_error(y_test, prediction))), 8),
            "mae": round(mean_absolute_error(y_test, prediction), 8),
        }

    resultats["modele_naif"] = {
        "r2": round(r2_score(y_test, naif_test), 6),
        "rmse": round(float(np.sqrt(mean_squared_error(y_test, naif_test))), 8),
        "mae": round(mean_absolute_error(y_test, naif_test), 8),
    }
    resultats["_lignes_test"] = len(y_test)
    return resultats


def main():
    mlflow.set_tracking_uri(SUIVI)
    mlflow.set_experiment("cryptobot_etape3")

    tout = {}
    for profil in config.TRADING_PROFILES:
        tout[profil] = {}
        for cible in ("prix", "rendement"):
            log.info("=== %s / cible : %s ===", profil, cible)
            resultats = evaluer(profil, cible)
            tout[profil][cible] = resultats

            meilleur = max(("ridge", "gradient_boosting"),
                           key=lambda m: resultats[m]["r2"])
            naif = resultats["modele_naif"]["r2"]

            with mlflow.start_run(
                run_name=f"regression_{profil}_{cible}_"
                         f"{datetime.now(timezone.utc):%Y%m%d_%H%M}"
            ):
                mlflow.log_params({
                    "profil": profil,
                    "type": "regression",
                    "cible": cible,
                    "lignes_test": resultats["_lignes_test"],
                })
                for nom in ("ridge", "gradient_boosting", "modele_naif"):
                    for mesure, valeur in resultats[nom].items():
                        mlflow.log_metric(f"{nom}_{mesure}", valeur)
                # Le seul chiffre qui compte : ce que le modele apporte
                # au-dela de la prediction naive.
                mlflow.log_metric("r2_gain_sur_naif",
                                  resultats[meilleur]["r2"] - naif)

            for nom in ("ridge", "gradient_boosting", "modele_naif"):
                r = resultats[nom]
                log.info("  %-20s R2 %10.6f   RMSE %.6f   MAE %.6f",
                         nom, r["r2"], r["rmse"], r["mae"])
            log.info("  gain du meilleur modele sur le naif : %+.6f",
                     resultats[meilleur]["r2"] - naif)

    (config.DOCS / "regression_baseline.json").write_text(
        json.dumps(tout, indent=2), encoding="utf-8"
    )
    log.info("Resultats ecrits : %s", config.DOCS / "regression_baseline.json")


if __name__ == "__main__":
    main()
