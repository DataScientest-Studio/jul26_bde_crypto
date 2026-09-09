"""Entrainement final : un modele par profil, suivi dans MLflow, exporte en .joblib.

MLflow enregistre pour chaque execution les parametres, les metriques et le
modele lui-meme. L'interet ici n'est pas la demonstration d'outil : c'est de
pouvoir repondre dans trois semaines a "d'ou vient ce fichier .joblib, sur
quelles donnees, avec quels reglages, et quel score ?".

Chaque execution enregistre aussi l'empreinte SHA-256 de l'extrait fige. Un
modele dont on ne sait pas sur quelles donnees il a ete entraine n'est pas
reproductible, et donc pas defendable.

Usage :
    python -m scripts.train_final
    python -m scripts.train_final --profils swing
    mlflow ui --backend-store-uri sqlite:///mlflow.db   # pour consulter
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import joblib
import mlflow
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src import config
from src.backtest import simuler
from src.features import FAMILLES, colonnes_explicatives, construire_groupes
from src.labeling import etiqueter_groupes
from src.preprocessing import INTERVAL_SECONDS
from src.scoring import resume

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("entrainement")

MODELES_DIR = config.ROOT / "models"
# MLflow 3.16 a mis le stockage fichier en maintenance : il exige desormais
# une base. SQLite convient parfaitement ici - un seul fichier, aucun serveur
# a lancer, et l'interface web le lit directement.
SUIVI = f"sqlite:///{(config.ROOT / 'mlflow.db').as_posix()}"
ARTEFACTS = config.ROOT / "mlartifacts"

# Gagnants de la comparaison, avec les parametres issus de l'optimisation.
CONFIGURATIONS = {
    "scalping": {
        "modele": LogisticRegression(max_iter=1000, C=1.0),
        "largeur": 3.0,
        "horizon": 12,
    },
    "day_trading": {
        "modele": HistGradientBoostingClassifier(
            random_state=0, max_iter=100, max_leaf_nodes=31, learning_rate=0.1
        ),
        "largeur": 3.0,
        "horizon": 12,
    },
    "swing": {
        "modele": RandomForestClassifier(
            n_jobs=-1, random_state=0, n_estimators=100, min_samples_leaf=20
        ),
        # Le swing est le seul profil ou l'elargissement des barrieres
        # rapproche du seuil de rentabilite (voir docs/).
        "largeur": 5.0,
        "horizon": 48,
    },
}


def empreinte_extrait(profil: str) -> str:
    """Empreinte de l'extrait fige, pour tracer sur quoi le modele a appris."""
    manifeste = json.loads(
        (config.ROOT / "data" / "extract" / "manifest.json").read_text(encoding="utf-8")
    )
    return manifeste["profils"][profil]["sha256"]


def preparer(profil: str, largeur: float, horizon: int) -> pd.DataFrame:
    brut = pd.read_parquet(config.ROOT / "data" / "extract" / f"{profil}.parquet")
    etiquettes = etiqueter_groupes(brut, largeur=largeur, horizon=horizon)
    variables = construire_groupes(brut, FAMILLES)
    cle = ["symbol", "interval", "open_time"]
    jeu = variables.merge(
        etiquettes[cle + ["label", "rendement_a_la_sortie", "bougies_avant_sortie"]],
        on=cle, how="inner",
    )
    jeu = jeu.dropna(subset=["label"]).sort_values("open_time").reset_index(drop=True)
    if len(jeu) > 150_000:
        jeu = jeu.tail(150_000).reset_index(drop=True)
    jeu["pas_de_temps"] = np.log(jeu["interval"].map(INTERVAL_SECONDS))
    return jeu


def entrainer(profil: str, configuration: dict) -> dict:
    jeu = preparer(profil, configuration["largeur"], configuration["horizon"])
    sorties = {"rendement_a_la_sortie", "bougies_avant_sortie"}
    colonnes = [c for c in colonnes_explicatives(jeu) if c not in sorties]
    X, y = jeu[colonnes], jeu["label"].astype(int)
    coupure = int(len(X) * 0.8)

    pipeline = Pipeline([
        ("imputation", SimpleImputer(strategy="median")),
        ("normalisation", StandardScaler()),
        ("modele", configuration["modele"]),
    ])

    with mlflow.start_run(run_name=f"{profil}_{datetime.now(timezone.utc):%Y%m%d_%H%M}"):
        mlflow.log_params({
            "profil": profil,
            "modele": type(configuration["modele"]).__name__,
            "largeur_barrieres_sigma": configuration["largeur"],
            "horizon_bougies": configuration["horizon"],
            "lignes_entrainement": coupure,
            "lignes_test": len(X) - coupure,
            "variables": len(colonnes),
            "pas_de_temps": ",".join(sorted(jeu["interval"].unique())),
            # Sans cette empreinte, impossible de savoir sur quelles donnees
            # exactes ce modele a ete entraine.
            "extrait_sha256": empreinte_extrait(profil),
            "decoupage": "chronologique 80/20",
        })
        mlflow.log_params({
            f"hp_{k}": v for k, v in configuration["modele"].get_params().items()
            if k in ("C", "max_iter", "max_leaf_nodes", "learning_rate",
                     "n_estimators", "min_samples_leaf")
        })

        pipeline.fit(X.iloc[:coupure], y.iloc[:coupure])
        prediction = pipeline.predict(X.iloc[coupure:])
        y_test = y.iloc[coupure:]
        test = jeu.iloc[coupure:].reset_index(drop=True)

        mesures = resume(y_test, prediction)
        backtest = simuler(prediction, test["rendement_a_la_sortie"],
                           test["bougies_avant_sortie"], fraction_engagee=0.10)

        mlflow.log_metrics({
            # Le bon sens directionnel est la metrique qui compte : l'accuracy
            # globale recompense la detection des phases calmes.
            "bon_sens_directionnel": mesures["bon_sens"],
            "accuracy_globale": mesures["accuracy_globale"],
            "taux_activite": mesures["taux_activite"],
            "f1_macro": f1_score(y_test, prediction, average="macro"),
            "backtest_operations": backtest["operations"],
            "backtest_gain_net_par_operation_pct": backtest["gain_moyen_net_pct"],
            "backtest_performance_pct": backtest["performance_pct"],
            "backtest_perte_max_pct": backtest["perte_max_pct"],
        })

        # MLflow 3.16 serialise via skops, qui refuse par defaut tout type
        # non explicitement autorise. numpy.dtype vient de l'imputation et de
        # la normalisation : il est attendu et sans risque ici.
        mlflow.sklearn.log_model(
            pipeline, name="pipeline", skops_trusted_types=["numpy.dtype"]
        )

        MODELES_DIR.mkdir(parents=True, exist_ok=True)
        chemin = MODELES_DIR / f"{profil}.joblib"
        joblib.dump({
            "pipeline": pipeline,
            "colonnes": colonnes,
            "profil": profil,
            "largeur_barrieres": configuration["largeur"],
            "horizon": configuration["horizon"],
            "extrait_sha256": empreinte_extrait(profil),
            "entraine_le": datetime.now(timezone.utc).isoformat(),
            "mesures": mesures,
        }, chemin)
        mlflow.log_artifact(str(chemin))

        log.info("  bon sens %.4f | activite %.1f %% | backtest %+.1f %%",
                 mesures["bon_sens"], mesures["taux_activite"] * 100,
                 backtest["performance_pct"])
        log.info("  exporte : %s (%.0f ko)", chemin, chemin.stat().st_size / 1024)

        return {"mesures": mesures, "backtest": backtest, "fichier": chemin.name}


def main():
    parser = argparse.ArgumentParser(description="Entrainement final et export")
    parser.add_argument("--profils", nargs="+", default=list(CONFIGURATIONS))
    args = parser.parse_args()

    mlflow.set_tracking_uri(SUIVI)
    ARTEFACTS.mkdir(parents=True, exist_ok=True)
    # `set_experiment` ne prend pas artifact_location dans MLflow 3.16 :
    # on cree l'experience explicitement si elle n'existe pas encore.
    if mlflow.get_experiment_by_name("cryptobot_etape3") is None:
        mlflow.create_experiment("cryptobot_etape3",
                                 artifact_location=ARTEFACTS.as_uri())
    mlflow.set_experiment("cryptobot_etape3")

    resultats = {}
    for profil in args.profils:
        log.info("=== %s ===", profil)
        resultats[profil] = entrainer(profil, CONFIGURATIONS[profil])

    (config.DOCS / "modeles_finaux.json").write_text(
        json.dumps(resultats, indent=2, default=str), encoding="utf-8"
    )
    log.info("Suivi MLflow : %s", SUIVI)
    log.info("Pour consulter : mlflow ui --backend-store-uri sqlite:///mlflow.db")


if __name__ == "__main__":
    main()
