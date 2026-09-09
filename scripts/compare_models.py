"""Comparaison de plusieurs modeles, sur les trois profils.

Protocole :
  - donnees issues de l'extrait FIGE, empreinte verifiee avant de commencer
  - etiquetage par trois barrieres
  - decoupage CHRONOLOGIQUE (jamais aleatoire : ce sont des series temporelles)
  - normalisation dans le Pipeline, donc ajustee sur l'entrainement seul
  - deux references obligatoires en face de chaque modele

Les references sont le coeur du protocole. Un modele a 52 % d'accuracy
parait bon jusqu'a ce qu'on remarque que predire toujours la classe
majoritaire en donne 50 %. Sans ce point de comparaison, on ne mesure rien.

Usage :
    python -m scripts.compare_models
    python -m scripts.compare_models --profils day_trading
    python -m scripts.compare_models --par-famille
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
import pandas as pd
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, SGDClassifier
from sklearn.metrics import accuracy_score, f1_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import LinearSVC

from src import config
from src.features import FAMILLES, colonnes_explicatives, construire_groupes
from src.labeling import etiqueter_groupes
from src.preprocessing import INTERVAL_SECONDS

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("compare")

EXTRACT = config.ROOT / "data" / "extract"
RESULTATS = config.DOCS / "comparaison_modeles.json"

# Reglages d'etiquetage retenus apres l'etude empirique : 3 sigma donne les
# trois classes les plus equilibrees (~33 % chacune), et l'horizon compte
# peu puisque la sortie mediane est de 7 bougies.
LARGEUR = 3.0
HORIZON = 12

# Plafond de lignes par profil. Le scalping en compte 1,6 million : un
# RandomForest dessus prendrait des heures pour un gain nul a ce stade de
# comparaison. On garde les lignes les plus RECENTES, pas un echantillon
# aleatoire, pour ne pas casser la continuite temporelle.
PLAFOND = 150_000


def modeles(n: int) -> dict:
    """Modeles a comparer, choisis d'apres la carte scikit-learn.

    La carte oriente vers LinearSVC sous 100 000 echantillons, puis vers
    SGDClassifier au-dela. On ajoute deux ensemblistes, que la carte
    propose quand le lineaire ne suffit pas - ce qui est l'hypothese la plus
    probable sur des donnees de marche, ou les relations sont non lineaires.
    """
    communs = {
        "reference_majoritaire": DummyClassifier(strategy="most_frequent"),
        "reference_aleatoire": DummyClassifier(strategy="stratified", random_state=0),
        "regression_logistique": LogisticRegression(max_iter=1000, n_jobs=-1),
        "foret_aleatoire": RandomForestClassifier(
            n_estimators=100, min_samples_leaf=50, n_jobs=-1, random_state=0
        ),
        "gradient_boosting": HistGradientBoostingClassifier(
            max_iter=200, random_state=0
        ),
    }
    # Le lineaire a noyau explicite ne passe pas l'echelle : au-dela de
    # 100 000 lignes on prend sa version par descente de gradient.
    if n <= 100_000:
        communs["svm_lineaire"] = LinearSVC(max_iter=3000, dual="auto")
    else:
        communs["sgd_lineaire"] = SGDClassifier(
            loss="hinge", max_iter=1000, n_jobs=-1, random_state=0
        )
    return communs


def preparer(profil: str, familles: tuple[str, ...]) -> tuple[pd.DataFrame, pd.Series]:
    """Charge, etiquette et construit les variables d'un profil."""
    brut = pd.read_parquet(EXTRACT / f"{profil}.parquet")

    etiquettes = etiqueter_groupes(brut, largeur=LARGEUR, horizon=HORIZON)
    variables = construire_groupes(brut, familles)

    cle = ["symbol", "interval", "open_time"]
    jeu = variables.merge(etiquettes[cle + ["label"]], on=cle, how="inner")
    jeu = jeu.dropna(subset=["label"])

    # Le pas de temps devient une variable : c'est ce qui permet a UN modele
    # de "comprendre les profils" plutot que d'ignorer a quelle echelle il
    # travaille.
    # Echelle logarithmique : entre 1m (60 s) et 1w (604 800 s) il y a un
    # facteur 10 000. En valeur brute, le modele ne verrait qu'un pas de
    # temps enorme et six pas de temps ecrases a zero.
    jeu["pas_de_temps"] = np.log(jeu["interval"].map(INTERVAL_SECONDS))

    jeu = jeu.sort_values("open_time").reset_index(drop=True)
    if len(jeu) > PLAFOND:
        jeu = jeu.tail(PLAFOND).reset_index(drop=True)

    colonnes = colonnes_explicatives(jeu)
    return jeu[colonnes], jeu["label"].astype(int)


def evaluer(X: pd.DataFrame, y: pd.Series, part_test: float = 0.2) -> list[dict]:
    """Entraine et evalue chaque modele sur un decoupage chronologique.

    Le test est la PERIODE LA PLUS RECENTE, jamais un tirage aleatoire :
    melanger reviendrait a entrainer sur le futur pour predire le passe.
    """
    coupure = int(len(X) * (1 - part_test))
    X_train, X_test = X.iloc[:coupure], X.iloc[coupure:]
    y_train, y_test = y.iloc[:coupure], y.iloc[coupure:]

    lignes = []
    for nom, modele in modeles(len(X_train)).items():
        pipeline = Pipeline([
            # L'imputation et la normalisation sont DANS le pipeline : ainsi
            # elles sont ajustees sur l'entrainement seul. Les appliquer
            # avant le decoupage ferait fuiter les statistiques du test.
            ("imputation", SimpleImputer(strategy="median")),
            ("normalisation", StandardScaler()),
            ("modele", modele),
        ])
        t0 = time.time()
        pipeline.fit(X_train, y_train)
        prediction = pipeline.predict(X_test)
        lignes.append({
            "modele": nom,
            "accuracy": round(accuracy_score(y_test, prediction), 4),
            "f1_macro": round(f1_score(y_test, prediction, average="macro"), 4),
            "secondes": round(time.time() - t0, 1),
            "classes_predites": int(len(np.unique(prediction))),
        })
    return lignes


def main():
    parser = argparse.ArgumentParser(description="Comparaison de modeles")
    parser.add_argument("--profils", nargs="+", default=list(config.TRADING_PROFILES))
    parser.add_argument("--par-famille", action="store_true",
                        help="Mesurer aussi l'apport de chaque famille de variables")
    args = parser.parse_args()

    resultats = {"largeur": LARGEUR, "horizon": HORIZON, "plafond": PLAFOND,
                 "profils": {}}

    for profil in args.profils:
        log.info("=== %s ===", profil)
        X, y = preparer(profil, FAMILLES)
        repartition = y.value_counts(normalize=True).sort_index()
        log.info("%d lignes, %d variables | classes : %s",
                 len(X), X.shape[1],
                 " ".join(f"{int(k):+d}={v*100:.0f}%" for k, v in repartition.items()))

        lignes = evaluer(X, y)
        reference = max(l["accuracy"] for l in lignes if "reference" in l["modele"])
        for l in lignes:
            l["gain_sur_reference"] = round(l["accuracy"] - reference, 4)

        for l in sorted(lignes, key=lambda x: -x["accuracy"]):
            marque = "  <- reference" if "reference" in l["modele"] else ""
            log.info("  %-24s acc %.4f  F1 %.4f  gain %+.4f  %5.1fs%s",
                     l["modele"], l["accuracy"], l["f1_macro"],
                     l["gain_sur_reference"], l["secondes"], marque)

        resultats["profils"][profil] = {
            "lignes": len(X), "variables": X.shape[1],
            "repartition": {str(int(k)): round(v, 4) for k, v in repartition.items()},
            "modeles": lignes,
        }

        if args.par_famille:
            log.info("  -- apport de chaque famille (gradient boosting) --")
            apports = {}
            for famille in FAMILLES:
                Xf, yf = preparer(profil, (famille,))
                l = [r for r in evaluer(Xf, yf) if r["modele"] == "gradient_boosting"][0]
                apports[famille] = l["accuracy"]
                log.info("     %-12s acc %.4f  (%d variables)",
                         famille, l["accuracy"], Xf.shape[1])
            resultats["profils"][profil]["familles"] = apports

    RESULTATS.write_text(json.dumps(resultats, indent=2), encoding="utf-8")
    log.info("Resultats ecrits : %s", RESULTATS)


if __name__ == "__main__":
    main()
