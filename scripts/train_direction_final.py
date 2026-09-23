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
from src.preprocessing import INTERVAL_SECONDS
from scripts.backtest_direction import FRAIS, exposition_maximale, simuler
from scripts.pistes_amelioration import (
    RECENTES, STYLES, colonnes_de, construire, construire_colonnes_seules, entrainer,
)

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("final")

MODELES = config.ROOT / "models"
FICHIER = MODELES / "direction_day_trading.joblib"
RESULTATS = config.DOCS / "modele_final_direction.json"
SUIVI = f"sqlite:///{(config.ROOT / 'mlflow.db').as_posix()}"
ARTEFACTS = config.ROOT / "mlartifacts"
EXPERIENCE = "cryptobot_direction"


def seuils_par_cote(probabilites, niveau: float) -> dict:
    """Un seuil pour acheter, un autre pour vendre.

    POURQUOI DEUX SEUILS. Les probabilites du modele ne sont pas symetriques :
    elles descendent bas du cote baisse mais ne depassent jamais 0,564 du cote
    hausse. Avec un seuil unique sur la distance a 0,5, le style conservateur
    se retrouvait AU-DESSUS du maximum atteignable en achat : il ne pouvait
    structurellement jamais acheter et ne produisait que des ventes. Ce n'est
    pas un modele baissier - les hausses reelles sont a 50 % - c'est un modele
    plus sur de lui quand il annonce une baisse.

    Chaque cote recoit donc son propre seuil, regle pour declencher le meme
    nombre de signaux : la moitie du budget d'ordres pour chacun.
    """
    budget = max(int(len(probabilites) * niveau / 2), 1)
    hausse = probabilites[probabilites >= 0.5]
    baisse = probabilites[probabilites < 0.5]
    seuil_achat = (float(np.quantile(hausse, 1 - budget / len(hausse)))
                   if len(hausse) > budget else 0.5)
    seuil_vente = (float(np.quantile(baisse, budget / len(baisse)))
                   if len(baisse) > budget else 0.5)
    return {"achat": round(seuil_achat, 4), "vente": round(seuil_vente, 4)}


def decider(probabilites, seuils: dict):
    """+1 acheter, -1 vendre, 0 attendre."""
    return np.where(probabilites >= seuils["achat"], 1,
                    np.where(probabilites <= seuils["vente"], -1, 0))


def mesurer_deux_seuils(probabilites, y, rendements, seuils, jours) -> dict:
    """Memes mesures qu'avec un seuil unique, mais sur les deux cotes."""
    from scripts.profils_de_risque import wilson

    sens = decider(probabilites, seuils)
    agit = sens != 0
    n = int(agit.sum())
    if not n:
        return {"ordres": 0}
    juste = (sens[agit] > 0) == (y[agit] == 1)
    brut = rendements[agit] * sens[agit]
    bas, haut = wilson(int(juste.sum()), n)
    return {
        "ordres": n,
        "achats": int((sens == 1).sum()),
        "ventes": int((sens == -1).sum()),
        "ordres_par_jour": round(n / jours, 2),
        "accuracy": round(float(juste.mean()), 4),
        "ic95": [round(bas, 4), round(haut, 4)],
        "rendement_moyen_brut_pct": round(float(brut.mean() * 100), 4),
        "rendement_moyen_net_pct": round(float((brut.mean() - 0.002) * 100), 4),
    }


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

    proba = modele.predict_proba(recentes[colonnes])[:, 1]
    y = recentes["label"].to_numpy()
    rendements = recentes["rendement_suivant"].to_numpy()
    accuracy_globale = float(((proba >= 0.5).astype(int) == y).mean())

    probabilites_seuils = modele.predict_proba(periode_seuils[colonnes])[:, 1]
    styles, mesures_mlflow = {}, {"accuracy_globale": accuracy_globale}
    for style, niveau in STYLES.items():
        seuils = seuils_par_cote(probabilites_seuils, niveau)
        mesure = mesurer_deux_seuils(proba, y, rendements, seuils, jours)
        # Backtest : 5 % du capital par ordre, frais standard, achat ET vente
        # (donc futures ; sur le spot, seuls les ordres a la hausse existent).
        sens = decider(proba, seuils)
        duree = pd.to_timedelta(recentes["interval"].map(INTERVAL_SECONDS), unit="s")
        ordres = pd.DataFrame({
            "sens": sens.astype(float), "rendement": rendements,
            "entree": (recentes["open_time"] + duree).to_numpy(),
            "sortie": (recentes["open_time"] + 2 * duree).to_numpy(),
        })[sens != 0]
        backtest = {cle: simuler(ordres, f) for cle, f in FRAIS.items()}
        expo = exposition_maximale(ordres["entree"].to_numpy(), ordres["sortie"].to_numpy()) \
            if len(ordres) else 0

        styles[style] = {"seuils": seuils,
                         "part_des_bougies_visee_pct": niveau * 100,
                         "mesures_bougies_jamais_vues": mesure,
                         "backtest_bougies_jamais_vues": backtest,
                         "exposition_maximale_pct": expo * 5}
        mesures_mlflow.update({
            f"{style}_seuil_achat": seuils["achat"],
            f"{style}_seuil_vente": seuils["vente"],
            f"{style}_achats": mesure["achats"],
            f"{style}_ventes": mesure["ventes"],
            f"{style}_accuracy": mesure["accuracy"],
            f"{style}_accuracy_ic95_bas": mesure["ic95"][0],
            f"{style}_accuracy_ic95_haut": mesure["ic95"][1],
            f"{style}_ordres_par_jour": mesure["ordres_par_jour"],
            f"{style}_gain_net_par_ordre_pct": mesure["rendement_moyen_net_pct"],
            f"{style}_backtest_performance_pct": backtest["standard_0.10%"]["performance_pct"],
        })
        log.info("  %-13s achat >= %.4f | vente <= %.4f | %4d ordres (%4d achats, %4d ventes, "
                 "%5.1f/jour) | accuracy %.4f [%.3f-%.3f] | backtest %+.2f %%",
                 style, seuils["achat"], seuils["vente"], mesure["ordres"], mesure["achats"],
                 mesure["ventes"], mesure["ordres_par_jour"], mesure["accuracy"], *mesure["ic95"],
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
            # Les arbres de la foret : types de scikit-learn lui-meme. skops 0.15
            # ne les accepte plus sans declaration explicite.
            "sklearn.tree._tree.Tree",
        ])

        MODELES.mkdir(exist_ok=True)
        joblib.dump({
            "modele": modele,
            "colonnes": colonnes,
            "cible": "sens de la prochaine bougie",
            "profil": "day_trading",
            "styles": {s: v["seuils"] for s, v in styles.items()},
            "style_le_plus_prudent": "conservateur",
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
