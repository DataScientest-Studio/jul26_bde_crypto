"""Modele FINAL : sens de la prochaine bougie, day trading, avec les deux styles.

Ce que ce script produit :
    - un modele entraine puis CALIBRE, suivi dans MLflow ;
    - les deux seuils du bouton (agressif / conservateur), fixes sur une
      periode dediee ;
    - models/direction_day_trading.joblib, qui contient le modele, la liste
      des variables, les seuils et les mesures. C'est ce fichier que l'API de
      l'etape 4 chargera.

CHOIX RETENUS, ET POURQUOI (tout est mesure dans docs/)
    cible       le sens de la prochaine bougie. C'est la seule cible ou le
                modele garde un avantage sur des bougies jamais vues ; a 4 h
                et a 1 jour il retombe au niveau du hasard (docs/horizon_long.json).
    modele      foret aleatoire (400 arbres, feuilles >= 100), reglages issus
                de docs/optimisation_direction.json.
    calibration isotonique, sur une periode que le modele n'a pas vue. Sans
                elle, ses probabilites extremes sont les moins fiables et
                l'accuracy CHUTE quand on devient tres selectif.
    variables   les 26 indicateurs + le contexte 1h/4h. Le carnet d'ordres,
                les valeurs des bougies precedentes, l'heure et l'etat du BTC
                n'ont rien apporte de regulier (docs/pistes_amelioration.json,
                docs/direction_prochaine_bougie.json).

DECOUPAGE CHRONOLOGIQUE
    0-72 %    apprentissage
    72-80 %   calibration
    80-100 %  seuils des deux styles
    bougies posterieures a l'extrait fige : mesure finale, jamais vues.

HONNETETE DU RESULTAT
    Le modele a un vrai avantage directionnel (environ 57 %), mais il n'est
    PAS rentable : le mouvement moyen d'une bougie 15m (0,227 %) est a peine
    superieur aux frais d'un aller-retour (0,2 %). Le backtest est enregistre
    dans MLflow au meme titre que l'accuracy, pour que ce soit visible.

Usage :
    python -m scripts.train_direction_final
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import joblib
import mlflow
import numpy as np
import pandas as pd

from src import config
from scripts.backtest_direction import FRAIS, construire_ordres, exposition_maximale, simuler
from scripts.pistes_amelioration import (
    RECENTES, STYLES, colonnes_de, construire, construire_colonnes_seules, entrainer,
)
from scripts.profils_de_risque import mesurer

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("final")

MODELES = config.ROOT / "models"
FICHIER = MODELES / "direction_day_trading.joblib"
RESULTATS = config.DOCS / "modele_final_direction.json"
SUIVI = f"sqlite:///{(config.ROOT / 'mlflow.db').as_posix()}"
ARTEFACTS = config.ROOT / "mlartifacts"
EXPERIENCE = "cryptobot_direction"


def empreintes() -> dict:
    """Sur quelles donnees exactes ce modele a-t-il appris et ete mesure ?"""
    manifeste = json.loads(
        (config.ROOT / "data" / "extract" / "manifest.json").read_text(encoding="utf-8"))
    recentes = json.loads((RECENTES.parent / "manifest.json").read_text(encoding="utf-8"))
    return {"extrait_sha256": manifeste["profils"]["day_trading"]["sha256"],
            "bougies_recentes_sha256": recentes["sha256"],
            "bougies_recentes_periode": recentes["periode_de_test"]}


def main():
    debut = time.time()
    extrait = pd.read_parquet(config.ROOT / "data" / "extract" / "day_trading.parquet")
    jeu = construire(extrait, set())
    colonnes = colonnes_de(construire_colonnes_seules(jeu, set()))

    n = len(jeu)
    i72, i80 = int(n * 0.72), int(n * 0.80)
    apprentissage, calibration, periode_seuils = jeu.iloc[:i72], jeu.iloc[i72:i80], jeu.iloc[i80:]

    recentes = construire(pd.read_parquet(RECENTES), set())
    recentes = recentes[recentes["open_time"] > extrait["open_time"].max()].reset_index(drop=True)
    jours = (recentes["open_time"].max() - recentes["open_time"].min()).total_seconds() / 86400

    log.info("apprentissage %d | calibration %d | seuils %d | bougies jamais vues %d (%.1f jours)",
             len(apprentissage), len(calibration), len(periode_seuils), len(recentes), jours)

    modele = entrainer(apprentissage[colonnes], apprentissage["label"],
                       calibration[colonnes], calibration["label"])
    log.info("Modele entraine et calibre en %.0f s", time.time() - debut)

    confiance_seuils = np.abs(modele.predict_proba(periode_seuils[colonnes])[:, 1] - 0.5)
    proba = modele.predict_proba(recentes[colonnes])[:, 1]
    y = recentes["label"].to_numpy()
    rendements = recentes["rendement_suivant"].to_numpy()
    accuracy_globale = float(((proba >= 0.5).astype(int) == y).mean())

    styles, mesures_mlflow = {}, {"accuracy_globale": accuracy_globale}
    for style, niveau in STYLES.items():
        seuil = float(np.quantile(confiance_seuils, 1 - niveau))
        mesure = mesurer(proba, y, rendements, seuil, jours)
        # Backtest : 5 % du capital par ordre, frais standard, achat ET vente
        # (donc futures ; sur le spot, seuls les ordres a la hausse existent).
        ordres = construire_ordres(modele, recentes, colonnes, seuil, "acheteur_vendeur")
        backtest = {cle: simuler(ordres, f) for cle, f in FRAIS.items()}
        expo = exposition_maximale(ordres["entree"].to_numpy(), ordres["sortie"].to_numpy()) \
            if len(ordres) else 0

        styles[style] = {"seuil_probabilite": round(0.5 + seuil, 4),
                         "part_des_bougies_visee_pct": niveau * 100,
                         "mesures_bougies_jamais_vues": mesure,
                         "backtest_bougies_jamais_vues": backtest,
                         "exposition_maximale_pct": expo * 5}
        mesures_mlflow.update({
            f"{style}_seuil": 0.5 + seuil,
            f"{style}_accuracy": mesure["accuracy"],
            f"{style}_accuracy_ic95_bas": mesure["ic95"][0],
            f"{style}_accuracy_ic95_haut": mesure["ic95"][1],
            f"{style}_ordres_par_jour": mesure["ordres_par_jour"],
            f"{style}_gain_net_par_ordre_pct": mesure["rendement_moyen_net_pct"],
            f"{style}_backtest_performance_pct": backtest["standard_0.10%"]["performance_pct"],
        })
        log.info("  %-13s p >= %.4f | %4d ordres (%5.1f/jour) | accuracy %.4f [%.3f-%.3f] | backtest %+.2f %%",
                 style, styles[style]["seuil_probabilite"], mesure["ordres"],
                 mesure["ordres_par_jour"], mesure["accuracy"], *mesure["ic95"],
                 backtest["standard_0.10%"]["performance_pct"])

    traces = empreintes()
    mlflow.set_tracking_uri(SUIVI)
    ARTEFACTS.mkdir(parents=True, exist_ok=True)
    if mlflow.get_experiment_by_name(EXPERIENCE) is None:
        mlflow.create_experiment(EXPERIENCE, artifact_location=ARTEFACTS.as_uri())
    mlflow.set_experiment(EXPERIENCE)

    with mlflow.start_run(run_name=f"direction_day_trading_{datetime.now(timezone.utc):%Y%m%d_%H%M}"):
        mlflow.log_params({
            "cible": "sens de la prochaine bougie",
            "profil": "day_trading",
            "pas_de_temps": "15m,1h,4h",
            "modele": "RandomForestClassifier",
            "calibration": "isotonique",
            "n_estimators": 400, "min_samples_leaf": 100, "max_features": "sqrt",
            "variables": len(colonnes),
            "contexte_multi_echelles": True,
            "decoupage": "chronologique 72/8/20 + bougies posterieures a l'extrait",
            "lignes_apprentissage": len(apprentissage),
            "lignes_calibration": len(calibration),
            "bougies_de_mesure": len(recentes),
            **{k: str(v) for k, v in traces.items()},
        })
        mlflow.log_metrics(mesures_mlflow)
        # MLflow serialise via skops, qui refuse tout type non declare. Ceux-ci
        # viennent de la chaine (imputation, normalisation) et de la
        # calibration isotonique : ils sont attendus et sans risque ici.
        mlflow.sklearn.log_model(modele, name="modele", skops_trusted_types=[
            "numpy.dtype",
            "sklearn.calibration._CalibratedClassifier",
            "sklearn.isotonic.IsotonicRegression",
            "sklearn.frozen._frozen.FrozenEstimator",
            "sklearn.preprocessing._label.LabelEncoder",
        ])

        MODELES.mkdir(exist_ok=True)
        joblib.dump({
            "modele": modele,
            "colonnes": colonnes,
            "cible": "sens de la prochaine bougie",
            "profil": "day_trading",
            "styles": {s: v["seuil_probabilite"] for s, v in styles.items()},
            # Au-dela, le modele est trop sur de lui pour rester fiable.
            "seuil_maximal": styles["conservateur"]["seuil_probabilite"],
            "mesures": styles,
            "accuracy_globale": accuracy_globale,
            "entraine_le": datetime.now(timezone.utc).isoformat(),
            **traces,
        }, FICHIER)
        mlflow.log_artifact(str(FICHIER))

    RESULTATS.write_text(json.dumps({
        "cible": "sens de la prochaine bougie", "profil": "day_trading",
        "variables": len(colonnes), "accuracy_globale": round(accuracy_globale, 4),
        "jours_de_mesure": round(jours, 1), "bougies_de_mesure": len(recentes),
        "styles": styles, **traces,
    }, indent=2), encoding="utf-8")

    log.info("accuracy sur toutes les bougies jamais vues : %.4f", accuracy_globale)
    log.info("Exporte : %s (%.0f ko)", FICHIER, FICHIER.stat().st_size / 1024)
    log.info("MLflow : experience %s | %s", EXPERIENCE, SUIVI)


if __name__ == "__main__":
    main()
