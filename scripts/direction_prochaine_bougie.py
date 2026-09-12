"""Peut-on predire le SENS de la prochaine bougie a 0,60 ?

Cible demandee en reunion : non plus le resultat d'un trade a barrieres, mais
simplement "la prochaine bougie monte-t-elle ou baisse-t-elle ?".

Le test mesure deux choses, et la seconde est la plus importante :

  1. l'accuracy sur TOUTES les bougies ;
  2. l'accuracy sur les seules bougies ou le modele est le plus SUR de lui.

Le point 2 est la traduction chiffree de "le modele peut attendre 80 % du
temps". On classe les predictions par confiance, on ne garde que les N % les
plus sures, et on regarde si l'accuracy monte. Si elle monte, la selectivite
paye et le bouton conservateur/agressif a un sens. Si elle reste plate, le
modele est sur de lui sans raison - ce qui est le cas le plus frequent sur
des donnees de marche.

Trois garde-fous, identiques aux autres scripts du projet :

  - decoupage CHRONOLOGIQUE : on teste sur la periode la plus recente ;
  - reference majoritaire systematique : sur un marche haussier, "toujours
    monter" atteint deja plus de 50 % ;
  - rendement moyen des positions prises, frais compris : une accuracy peut
    monter alors que l'argent part, si le modele ne gagne que sur les
    petites bougies et perd sur les grosses.

Usage :
    python -m scripts.direction_prochaine_bougie
    python -m scripts.direction_prochaine_bougie --profils day_trading swing
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
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src import config
from src.features import (
    FAMILLES, ajouter_contexte_lent, colonnes_explicatives, construire_groupes,
)
from src.preprocessing import INTERVAL_SECONDS

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("direction")

EXTRACT = config.ROOT / "data" / "extract"
RESULTATS = config.DOCS / "direction_prochaine_bougie.json"

# Frais Binance : 0,1 % a l'entree + 0,1 % a la sortie.
FRAIS_ALLER_RETOUR = 0.002

# Taux d'activite testes : 100 % = le modele se prononce toujours,
# 5 % = il ne garde que les 5 % de bougies ou il est le plus sur.
ACTIVITES = (1.00, 0.50, 0.20, 0.10, 0.05, 0.02, 0.01)


def etiqueter_direction(brut: pd.DataFrame) -> pd.DataFrame:
    """Etiquette chaque bougie par le sens de la SUIVANTE.

    `pct_change().shift(-1)` place sur la bougie t le rendement de t+1. Les
    variables explicatives de t n'utilisent que le passe : il n'y a donc pas
    de fuite, la cible est bien dans le futur et elle seule.
    """
    morceaux = []
    for (symbole, pas), groupe in brut.groupby(["symbol", "interval"], sort=False):
        groupe = groupe.sort_values("open_time")
        morceaux.append(pd.DataFrame({
            "symbol": symbole,
            "interval": pas,
            "open_time": groupe["open_time"].to_numpy(),
            "rendement_suivant": groupe["close"].pct_change().shift(-1).to_numpy(),
        }))
    return pd.concat(morceaux, ignore_index=True)


def preparer(profil: str, contexte: bool = False) -> pd.DataFrame:
    """Variables explicatives + sens de la prochaine bougie."""
    brut = pd.read_parquet(EXTRACT / f"{profil}.parquet")
    cible = etiqueter_direction(brut)
    variables = construire_groupes(brut, FAMILLES)
    if contexte:
        # Chaque bougie recoit l'etat des pas de temps plus lents. Le
        # rattachement se fait sur leur CLOTURE, sinon c'est une fuite :
        # voir le commentaire dans src/features.py.
        variables = ajouter_contexte_lent(variables)

    cle = ["symbol", "interval", "open_time"]
    jeu = variables.merge(cible, on=cle, how="inner")
    jeu = jeu.dropna(subset=["rendement_suivant"])
    # Une bougie qui cloture exactement au meme prix n'a pas de sens a predire.
    jeu = jeu[jeu["rendement_suivant"] != 0]

    jeu["label"] = (jeu["rendement_suivant"] > 0).astype(int)
    jeu["pas_de_temps"] = np.log(jeu["interval"].map(INTERVAL_SECONDS))
    return jeu.sort_values("open_time").reset_index(drop=True)


def modeles() -> dict:
    return {
        "reference_majoritaire": DummyClassifier(strategy="most_frequent"),
        "regression_logistique": LogisticRegression(max_iter=1000, n_jobs=-1),
        "gradient_boosting": HistGradientBoostingClassifier(max_iter=200, random_state=0),
        "foret_aleatoire": RandomForestClassifier(
            n_estimators=100, min_samples_leaf=50, n_jobs=-1, random_state=0
        ),
    }


def par_confiance(probabilites: np.ndarray, y_vrai: np.ndarray,
                  rendements: np.ndarray) -> list[dict]:
    """Accuracy et rendement quand on ne garde que les predictions les plus sures.

    La confiance d'une prediction binaire est la distance a 0,5 : une
    probabilite de 0,52 comme de 0,48 signifie "je ne sais pas".
    """
    prediction = (probabilites >= 0.5).astype(int)
    confiance = np.abs(probabilites - 0.5)
    ordre = np.argsort(-confiance)  # du plus sur au moins sur

    lignes = []
    for activite in ACTIVITES:
        garde = ordre[:max(int(len(ordre) * activite), 1)]
        juste = prediction[garde] == y_vrai[garde]
        # Rendement de la position, orientee selon la prediction : on gagne le
        # rendement si on a predit la hausse, son oppose si on a predit la baisse.
        sens = np.where(prediction[garde] == 1, 1.0, -1.0)
        brut = rendements[garde] * sens
        lignes.append({
            "activite_pct": round(activite * 100, 1),
            "bougies": int(len(garde)),
            "accuracy": round(float(juste.mean()), 4),
            "seuil_confiance": round(float(confiance[garde].min() + 0.5), 4),
            "rendement_moyen_brut_pct": round(float(brut.mean() * 100), 4),
            "rendement_moyen_net_pct": round(float(brut.mean() - FRAIS_ALLER_RETOUR) * 100, 4),
        })
    return lignes


def evaluer(profil: str, contexte: bool = False, part_test: float = 0.2) -> dict:
    jeu = preparer(profil, contexte)
    hors_variables = {"rendement_suivant", "label"}
    colonnes = [c for c in colonnes_explicatives(jeu) if c not in hors_variables]

    X, y = jeu[colonnes], jeu["label"]
    coupure = int(len(X) * (1 - part_test))
    X_train, X_test = X.iloc[:coupure], X.iloc[coupure:]
    y_train, y_test = y.iloc[:coupure], y.iloc[coupure:]
    test = jeu.iloc[coupure:].reset_index(drop=True)

    part_hausse = float(y_test.mean())
    log.info("%s : %d lignes, %d variables | hausses dans le test : %.2f %%",
             profil, len(X), len(colonnes), part_hausse * 100)

    resultats = {
        "lignes": len(X),
        "variables": len(colonnes),
        "part_hausse_test": round(part_hausse, 4),
        # Une reference honnete : parier toujours dans le sens le plus frequent.
        "reference_toujours_meme_sens": round(max(part_hausse, 1 - part_hausse), 4),
        "modeles": {},
    }

    for nom, modele in modeles().items():
        pipeline = Pipeline([
            ("imputation", SimpleImputer(strategy="median")),
            ("normalisation", StandardScaler()),
            ("modele", modele),
        ])
        t0 = time.time()
        pipeline.fit(X_train, y_train)
        probabilites = pipeline.predict_proba(X_test)[:, 1]
        secondes = round(time.time() - t0, 1)

        niveaux = par_confiance(probabilites, y_test.to_numpy(),
                                test["rendement_suivant"].to_numpy())
        resultats["modeles"][nom] = {"secondes": secondes, "par_confiance": niveaux}

        globale = niveaux[0]
        log.info("  %-24s acc %.4f  (%ss)", nom, globale["accuracy"], secondes)
        for ligne in niveaux[1:]:
            log.info("        %5.1f %% des bougies : acc %.4f | net %+.3f %%/op",
                     ligne["activite_pct"], ligne["accuracy"],
                     ligne["rendement_moyen_net_pct"])

        # Detail par pas de temps sur le meilleur candidat non lineaire :
        # peut-etre que le 4h est previsible et le 15m non.
        if nom == "gradient_boosting":
            detail = {}
            prediction = (probabilites >= 0.5).astype(int)
            for pas in sorted(test["interval"].unique(), key=lambda i: INTERVAL_SECONDS[i]):
                masque = (test["interval"] == pas).to_numpy()
                detail[pas] = {
                    "bougies": int(masque.sum()),
                    "accuracy": round(float((prediction[masque] == y_test.to_numpy()[masque]).mean()), 4),
                }
                log.info("        pas %-4s : acc %.4f  (%d bougies)",
                         pas, detail[pas]["accuracy"], detail[pas]["bougies"])
            resultats["modeles"][nom]["par_pas_de_temps"] = detail

    return resultats


def main():
    parser = argparse.ArgumentParser(description="Prediction du sens de la prochaine bougie")
    parser.add_argument("--profils", nargs="+", default=["day_trading"])
    parser.add_argument("--contexte", action="store_true",
                        help="Ajouter l'etat des pas de temps plus lents")
    args = parser.parse_args()

    # Le fichier FUSIONNE les variantes : lancer la version avec contexte ne
    # doit pas effacer celle sans contexte, sinon les deux ne sont plus
    # comparables. Meme precaution que pour le rapport qualite de l'etape 1.
    resultats = {"cible": "sens de la prochaine bougie", "variantes": {}}
    if RESULTATS.exists():
        resultats = json.loads(RESULTATS.read_text(encoding="utf-8"))
        resultats.setdefault("variantes", {})
        # Restes de l'ancien format, a plat.
        resultats.pop("profils", None)
        resultats.pop("contexte_multi_echelles", None)

    variante = "avec_contexte" if args.contexte else "sans_contexte"
    profils = resultats["variantes"].setdefault(variante, {})

    for profil in args.profils:
        log.info("=== %s (contexte : %s) ===", profil, "oui" if args.contexte else "non")
        profils[profil] = evaluer(profil, args.contexte)

    RESULTATS.write_text(json.dumps(resultats, indent=2), encoding="utf-8")
    log.info("Resultats ecrits : %s", RESULTATS)


if __name__ == "__main__":
    main()
