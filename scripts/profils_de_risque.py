"""Du plus agressif au plus conservateur : que vaut chaque seuil de confiance ?

Le futur bouton "conservateur / agressif" du projet final sera un SEUIL DE
PROBABILITE : le bot ne passe un ordre que si le modele est sur de lui au-dela
de ce seuil. Seuil bas = beaucoup d'ordres, seuil haut = peu d'ordres.

Jusqu'ici on parlait de "garder les 2 % de bougies les plus sures". C'etait
pratique pour comparer des modeles, mais ce n'est pas utilisable en
production : pour savoir quelles bougies sont dans les 2 % les plus sures, il
faut les avoir TOUTES vues, futur compris. Le bot, lui, doit decider bougie
par bougie, avec un seuil fixe.

Protocole, en trois periodes chronologiques :

    entrainement  (0 - 64 %)   le modele apprend
    validation    (64 - 80 %)  on FIXE les seuils : "a quelle probabilite
                               correspond le top 2 %, 1 %, 0,5 % ... ?"
    test          (80 - 100 %) on APPLIQUE ces seuils tels quels, une fois

Sur le test, la part d'ordres n'est donc plus exactement 2 % : elle est ce
que le seuil donne reellement sur une periode inconnue. C'est la mesure
honnete de ce que ferait le bouton.

A chaque niveau on affiche l'intervalle de confiance a 95 % de l'accuracy :
sous quelques centaines d'ordres, il devient trop large pour conclure.

Usage :
    python -m scripts.profils_de_risque
"""
from __future__ import annotations

import json
import logging
import math
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src import config
from src.features import colonnes_explicatives
from scripts.direction_prochaine_bougie import FRAIS_ALLER_RETOUR, preparer

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("profils")

RESULTATS = config.DOCS / "profils_de_risque.json"

# Reglages retenus par l'optimisation a 2 % (docs/optimisation_direction.json).
PARAMETRES = dict(n_estimators=400, min_samples_leaf=100, max_features="sqrt",
                  n_jobs=-1, random_state=0)

# Part des bougies de VALIDATION ou le modele se prononce. Sert a fixer le seuil.
NIVEAUX = (0.20, 0.10, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001)

# Styles proposes pour le bouton.
#
# Premiere version, fixee avant l'experience : agressif 10 %, equilibre 2 %,
# conservateur 0,5 %. Elle a ete corrigee : sous 2 %, l'accuracy BAISSE au
# lieu de monter, et ce des la VALIDATION (0,5575 a 2 %, 0,5488 a 1 %,
# 0,5330 a 0,5 %, 0,4521 a 0,1 %). Les probabilites les plus extremes du
# modele sont donc les moins fiables : un style "conservateur" a 0,5 % serait
# a la fois moins actif ET moins precis. La correction s'appuie sur la
# validation seule, pas sur le test.
STYLES = {"agressif": 0.10, "conservateur": 0.02}

# Au-dela de ce seuil, le modele est trop sur de lui pour etre fiable : le
# bouton ne doit pas permettre d'aller plus haut.
SEUIL_MAXIMAL_NIVEAU = 0.02


def wilson(justes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalle de confiance a 95 % d'une proportion (methode de Wilson).

    Plus fiable que "p +- 1,96 x ecart-type" sur les petits echantillons, qui
    sont justement ceux des styles les plus conservateurs.
    """
    if total == 0:
        return (float("nan"), float("nan"))
    p = justes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    marge = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return (centre - marge, centre + marge)


def mesurer(probabilites, y, rendements, seuil_confiance: float, jours: float) -> dict:
    """Ce que donne un seuil fixe sur une periode."""
    confiance = np.abs(probabilites - 0.5)
    garde = confiance >= seuil_confiance
    n = int(garde.sum())
    if n == 0:
        return {"ordres": 0}

    prediction = (probabilites[garde] >= 0.5).astype(int)
    juste = prediction == y[garde]
    sens = np.where(prediction == 1, 1.0, -1.0)
    brut = rendements[garde] * sens
    bas, haut = wilson(int(juste.sum()), n)

    return {
        "ordres": n,
        "part_des_bougies_pct": round(100 * n / len(probabilites), 3),
        "ordres_par_jour": round(n / jours, 2),
        "accuracy": round(float(juste.mean()), 4),
        "ic95": [round(bas, 4), round(haut, 4)],
        "rendement_moyen_brut_pct": round(float(brut.mean() * 100), 4),
        "rendement_moyen_net_pct": round(float((brut.mean() - FRAIS_ALLER_RETOUR) * 100), 4),
    }


def main():
    debut = time.time()
    jeu = preparer("day_trading", contexte=True)
    colonnes = [c for c in colonnes_explicatives(jeu) if c not in {"rendement_suivant", "label"}]

    n = len(jeu)
    fin_train, fin_val = int(n * 0.64), int(n * 0.80)
    periodes = {"entrainement": jeu.iloc[:fin_train],
                "validation": jeu.iloc[fin_train:fin_val],
                "test": jeu.iloc[fin_val:]}
    for nom, p in periodes.items():
        log.info("%-12s %7d lignes  %s -> %s", nom, len(p),
                 p["open_time"].min().date(), p["open_time"].max().date())

    pipeline = Pipeline([
        ("imputation", SimpleImputer(strategy="median")),
        ("normalisation", StandardScaler()),
        ("modele", RandomForestClassifier(**PARAMETRES)),
    ])
    entrainement = periodes["entrainement"]
    pipeline.fit(entrainement[colonnes], entrainement["label"])
    log.info("Modele entraine en %.0f s", time.time() - debut)

    def preds(periode):
        p = periodes[periode]
        jours = (p["open_time"].max() - p["open_time"].min()).total_seconds() / 86400
        return (pipeline.predict_proba(p[colonnes])[:, 1], p["label"].to_numpy(),
                p["rendement_suivant"].to_numpy(), jours)

    proba_val, y_val, rend_val, jours_val = preds("validation")
    proba_test, y_test, rend_test, jours_test = preds("test")
    confiance_val = np.abs(proba_val - 0.5)

    lignes = []
    log.info("")
    log.info("%-7s %-8s | %-18s | %-48s | %s", "niveau", "seuil", "validation",
             "TEST (seuil applique tel quel)", "net/op")
    for niveau in NIVEAUX:
        # Le seuil est lu sur la VALIDATION : c'est la probabilite a partir
        # de laquelle le modele se prononcait sur `niveau` % des bougies.
        seuil_confiance = float(np.quantile(confiance_val, 1 - niveau))
        val = mesurer(proba_val, y_val, rend_val, seuil_confiance, jours_val)
        test = mesurer(proba_test, y_test, rend_test, seuil_confiance, jours_test)
        seuil_proba = 0.5 + seuil_confiance

        lignes.append({"niveau_validation_pct": niveau * 100,
                       "seuil_probabilite": round(seuil_proba, 4),
                       "validation": val, "test": test})
        if test["ordres"]:
            log.info("%6g%% %8.4f | acc %.4f (%5d)  | acc %.4f [%.3f-%.3f] %5d ordres %6.2f/jour | %+.3f %%",
                     niveau * 100, seuil_proba, val["accuracy"], val["ordres"],
                     test["accuracy"], test["ic95"][0], test["ic95"][1], test["ordres"],
                     test["ordres_par_jour"], test["rendement_moyen_net_pct"])
        else:
            log.info("%6g%% %8.4f | acc %.4f (%5d)  | aucun ordre sur le test",
                     niveau * 100, seuil_proba, val["accuracy"], val["ordres"])

    styles = {}
    for style, niveau in STYLES.items():
        ligne = next(l for l in lignes if abs(l["niveau_validation_pct"] - niveau * 100) < 1e-9)
        styles[style] = {"seuil_probabilite": ligne["seuil_probabilite"], "test": ligne["test"]}

    log.info("")
    log.info("Styles proposes pour le bouton :")
    for style, s in styles.items():
        t = s["test"]
        log.info("  %-12s probabilite >= %.4f -> %6.2f ordres/jour, accuracy %.4f [%.3f-%.3f]",
                 style, s["seuil_probabilite"], t["ordres_par_jour"], t["accuracy"],
                 t["ic95"][0], t["ic95"][1])

    seuil_max = next(l["seuil_probabilite"] for l in lignes
                     if abs(l["niveau_validation_pct"] - SEUIL_MAXIMAL_NIVEAU * 100) < 1e-9)
    log.info("  seuil maximal autorise : %.4f (au-dela, l'accuracy baisse)", seuil_max)

    RESULTATS.write_text(json.dumps({
        "modele": "RandomForestClassifier", "parametres": {k: v for k, v in PARAMETRES.items()
                                                            if k not in ("n_jobs",)},
        "contexte_multi_echelles": True,
        "periodes": {nom: {"lignes": len(p), "debut": str(p["open_time"].min()),
                           "fin": str(p["open_time"].max())} for nom, p in periodes.items()},
        "niveaux": lignes,
        "styles": styles,
        "seuil_maximal_autorise": seuil_max,
    }, indent=2), encoding="utf-8")
    log.info("Resultats ecrits : %s (%.0f s)", RESULTATS, time.time() - debut)


if __name__ == "__main__":
    main()
