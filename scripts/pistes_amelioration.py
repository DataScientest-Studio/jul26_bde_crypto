"""Trois pistes issues des recherches, puis un test sur des bougies jamais vues.

PISTES TESTEES
    calibration  les probabilites de la foret aleatoire sont trop tranchees
                 (connu dans la litterature) ; on les recale par regression
                 isotonique sur une periode dediee.
    calendrier   heure et jour de la semaine (effets documentes sur le BTC,
                 ex. heures 22-23 UTC).
    btc          etat du BTC sur la meme bougie pour toutes les paires : les
                 rendements passes des autres cryptos predisent les rendements
                 (litterature "cross-cryptocurrency return predictability").

PROTOCOLE, FIXE AVANT DE LANCER
    Phase A - choix, sur la VALIDATION uniquement :
        entrainement 0-64 %, validation 64-80 %.
        (variante calibree : entrainement 0-56 %, calibration 56-64 %)
        Une piste est retenue si elle ameliore l'accuracy selective a 2 % ET
        a 5 % par rapport a la reference. Le test historique (80-100 %) n'est
        pas regarde : il a deja servi a trop d'essais.

    Phase B - mesure finale, UNE fois, sur les bougies posterieures a
    l'extrait fige (scripts/nouvelles_bougies.py) :
        entrainement 0-80 % (0-72 % + calibration 72-80 % si retenue),
        seuils des styles fixes sur 80-100 %, appliques tels quels aux
        bougies recentes. La reference est mesuree a cote, pour comparer.

Usage :
    python -m scripts.nouvelles_bougies      # d'abord
    python -m scripts.pistes_amelioration
"""
from __future__ import annotations

import json
import logging
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src import config
from src.features import colonnes_explicatives
from scripts.direction_prochaine_bougie import FRAIS_ALLER_RETOUR, preparer_depuis
from scripts.profils_de_risque import mesurer, wilson

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pistes")

RESULTATS = config.DOCS / "pistes_amelioration.json"
RECENTES = config.DATA_PROCESSED / "nouvelles_bougies" / "day_trading_recent.parquet"
PARAMETRES = dict(n_estimators=400, min_samples_leaf=100, max_features="sqrt",
                  n_jobs=-1, random_state=0)
NIVEAUX = (0.10, 0.05, 0.02, 0.01)
STYLES = {"agressif": 0.10, "conservateur": 0.02}
HORS_VARIABLES = {"rendement_suivant", "label"}


# ---------------------------------------------------------------------------
#  Nouvelles variables
# ---------------------------------------------------------------------------

def ajouter_calendrier(jeu: pd.DataFrame) -> pd.DataFrame:
    """Heure et jour, encodes en cercle.

    Sinus et cosinus plutot que l'heure brute : pour le modele, 23h et 0h
    doivent etre voisins, pas aux deux extremites d'une echelle de 0 a 23.
    L'heure d'OUVERTURE de la bougie est connue au moment de decider : pas de
    fuite.
    """
    jeu = jeu.copy()
    heure = jeu["open_time"].dt.hour + jeu["open_time"].dt.minute / 60
    jour = jeu["open_time"].dt.dayofweek
    jeu["heure_sin"] = np.sin(2 * np.pi * heure / 24)
    jeu["heure_cos"] = np.cos(2 * np.pi * heure / 24)
    jeu["jour_sin"] = np.sin(2 * np.pi * jour / 7)
    jeu["jour_cos"] = np.cos(2 * np.pi * jour / 7)
    return jeu


def ajouter_btc(jeu: pd.DataFrame) -> pd.DataFrame:
    """Etat du BTC sur la MEME bougie (meme pas de temps, meme ouverture).

    La bougie BTC cloture exactement en meme temps que celle de la paire : son
    rendement est connu au moment de decider. On ajoute aussi l'ecart entre la
    paire et le BTC : une paire en retard sur le BTC va-t-elle rattraper ?
    """
    cle = ["interval", "open_time"]
    btc = jeu.loc[jeu["symbol"] == "BTCUSDT", cle + ["rendement_1", "rendement_3", "rsi_14"]]
    btc = btc.rename(columns={"rendement_1": "btc_rendement_1",
                              "rendement_3": "btc_rendement_3",
                              "rsi_14": "btc_rsi_14"})
    jeu = jeu.merge(btc, on=cle, how="left")
    jeu["ecart_vs_btc_1"] = jeu["rendement_1"] - jeu["btc_rendement_1"]
    jeu["ecart_vs_btc_3"] = jeu["rendement_3"] - jeu["btc_rendement_3"]
    return jeu


def construire(brut: pd.DataFrame, pistes: set[str]) -> pd.DataFrame:
    jeu = preparer_depuis(brut, contexte=True)
    if "calendrier" in pistes:
        jeu = ajouter_calendrier(jeu)
    if "btc" in pistes:
        jeu = ajouter_btc(jeu)
    return jeu.sort_values("open_time").reset_index(drop=True)


# ---------------------------------------------------------------------------
#  Modele et mesures
# ---------------------------------------------------------------------------

def entrainer(X, y, X_calibration=None, y_calibration=None):
    pipeline = Pipeline([
        ("imputation", SimpleImputer(strategy="median")),
        ("normalisation", StandardScaler()),
        ("modele", RandomForestClassifier(**PARAMETRES)),
    ])
    pipeline.fit(X, y)
    if X_calibration is None:
        return pipeline
    # Le modele est GELE, seule la correspondance probabilite -> frequence
    # observee est apprise, sur une periode que le modele n'a pas vue.
    calibre = CalibratedClassifierCV(FrozenEstimator(pipeline), method="isotonic")
    return calibre.fit(X_calibration, y_calibration)


def selectif(probabilites, y) -> dict:
    """Accuracy sur les N % de bougies les plus sures, avec intervalle a 95 %."""
    prediction = (probabilites >= 0.5).astype(int)
    ordre = np.argsort(-np.abs(probabilites - 0.5), kind="stable")
    sortie = {}
    for niveau in NIVEAUX:
        garde = ordre[:max(int(len(ordre) * niveau), 1)]
        justes = int((prediction[garde] == y[garde]).sum())
        bas, haut = wilson(justes, len(garde))
        sortie[f"{niveau * 100:g}%"] = {"accuracy": round(justes / len(garde), 4),
                                        "bougies": len(garde),
                                        "ic95": [round(bas, 4), round(haut, 4)]}
    return sortie


def colonnes_de(jeu):
    return [c for c in colonnes_explicatives(jeu) if c not in HORS_VARIABLES]


# ---------------------------------------------------------------------------
#  Phase A : choix des pistes sur la validation
# ---------------------------------------------------------------------------

def phase_a(extrait: pd.DataFrame) -> tuple[set[str], dict]:
    log.info("=== PHASE A : choix des pistes sur la VALIDATION ===")
    jeu = construire(extrait, {"calendrier", "btc"})
    n = len(jeu)
    i56, i64, i80 = int(n * 0.56), int(n * 0.64), int(n * 0.80)
    validation = jeu.iloc[i64:i80]
    y_val = validation["label"].to_numpy()
    base_cols = colonnes_de(construire_colonnes_seules(jeu, set()))

    variantes = {
        "reference": (base_cols, False),
        "calibration": (base_cols, True),
        "calendrier": (base_cols + ["heure_sin", "heure_cos", "jour_sin", "jour_cos"], False),
        "btc": (base_cols + ["btc_rendement_1", "btc_rendement_3", "btc_rsi_14",
                             "ecart_vs_btc_1", "ecart_vs_btc_3"], False),
    }

    resultats = {}
    for nom, (colonnes, calibrer) in variantes.items():
        t0 = time.time()
        if calibrer:
            modele = entrainer(jeu.iloc[:i56][colonnes], jeu.iloc[:i56]["label"],
                               jeu.iloc[i56:i64][colonnes], jeu.iloc[i56:i64]["label"])
        else:
            modele = entrainer(jeu.iloc[:i64][colonnes], jeu.iloc[:i64]["label"])
        proba = modele.predict_proba(validation[colonnes])[:, 1]
        resultats[nom] = selectif(proba, y_val)
        log.info("  %-12s %s  (%d variables, %.0f s)", nom,
                 " | ".join(f"{k} {v['accuracy']:.4f}" for k, v in resultats[nom].items()),
                 len(colonnes), time.time() - t0)

    retenues = set()
    ref = resultats["reference"]
    for nom in ("calibration", "calendrier", "btc"):
        gain_2 = resultats[nom]["2%"]["accuracy"] - ref["2%"]["accuracy"]
        gain_5 = resultats[nom]["5%"]["accuracy"] - ref["5%"]["accuracy"]
        garde = gain_2 > 0 and gain_5 > 0
        log.info("  %-12s gain a 2 %% : %+.4f | a 5 %% : %+.4f -> %s",
                 nom, gain_2, gain_5, "RETENUE" if garde else "ecartee")
        if garde:
            retenues.add(nom)
    return retenues, resultats


def construire_colonnes_seules(jeu, pistes):
    """Vue du jeu sans les colonnes des pistes non demandees."""
    a_retirer = []
    if "calendrier" not in pistes:
        a_retirer += ["heure_sin", "heure_cos", "jour_sin", "jour_cos"]
    if "btc" not in pistes:
        a_retirer += ["btc_rendement_1", "btc_rendement_3", "btc_rsi_14",
                      "ecart_vs_btc_1", "ecart_vs_btc_3"]
    return jeu.drop(columns=[c for c in a_retirer if c in jeu.columns])


# ---------------------------------------------------------------------------
#  Phase B : mesure finale sur les bougies recentes
# ---------------------------------------------------------------------------

def phase_b(extrait: pd.DataFrame, retenues: set[str]) -> dict:
    log.info("=== PHASE B : bougies jamais vues (pistes retenues : %s) ===",
             ", ".join(sorted(retenues)) or "aucune")
    recentes_brutes = pd.read_parquet(RECENTES)
    fin_extrait = extrait["open_time"].max()

    jeu = construire(extrait, {"calendrier", "btc"})
    recentes = construire(recentes_brutes, {"calendrier", "btc"})
    recentes = recentes[recentes["open_time"] > fin_extrait].reset_index(drop=True)
    jours = (recentes["open_time"].max() - recentes["open_time"].min()).total_seconds() / 86400
    log.info("  %d bougies recentes sur %.1f jours", len(recentes), jours)

    n = len(jeu)
    i72, i80 = int(n * 0.72), int(n * 0.80)
    seuils_sur = jeu.iloc[i80:]

    configurations = {"reference": (set(), False)}
    if retenues:
        configurations["avec_pistes_retenues"] = (retenues - {"calibration"},
                                                  "calibration" in retenues)

    sortie = {"bougies_recentes": len(recentes), "jours": round(jours, 1), "configurations": {}}
    for nom, (pistes, calibrer) in configurations.items():
        colonnes = colonnes_de(construire_colonnes_seules(jeu, pistes))
        if calibrer:
            modele = entrainer(jeu.iloc[:i72][colonnes], jeu.iloc[:i72]["label"],
                               jeu.iloc[i72:i80][colonnes], jeu.iloc[i72:i80]["label"])
        else:
            modele = entrainer(jeu.iloc[:i80][colonnes], jeu.iloc[:i80]["label"])

        confiance_seuils = np.abs(modele.predict_proba(seuils_sur[colonnes])[:, 1] - 0.5)
        proba = modele.predict_proba(recentes[colonnes])[:, 1]
        y = recentes["label"].to_numpy()
        rendements = recentes["rendement_suivant"].to_numpy()

        styles = {}
        for style, niveau in STYLES.items():
            seuil = float(np.quantile(confiance_seuils, 1 - niveau))
            styles[style] = {"seuil_probabilite": round(0.5 + seuil, 4),
                             **mesurer(proba, y, rendements, seuil, jours)}
            s = styles[style]
            if s["ordres"]:
                log.info("  %-22s %-12s p >= %.4f : %4d ordres (%5.1f/jour) acc %.4f [%.3f-%.3f] net %+.3f %%",
                         nom, style, s["seuil_probabilite"], s["ordres"], s["ordres_par_jour"],
                         s["accuracy"], s["ic95"][0], s["ic95"][1], s["rendement_moyen_net_pct"])
            else:
                log.info("  %-22s %-12s p >= %.4f : aucun ordre", nom, style, s["seuil_probabilite"])

        globale = float(((proba >= 0.5).astype(int) == y).mean())
        sortie["configurations"][nom] = {"variables": len(colonnes), "calibration": calibrer,
                                         "accuracy_globale": round(globale, 4), "styles": styles}
        log.info("  %-22s accuracy globale %.4f", nom, globale)
    return sortie


def main():
    debut = time.time()
    extrait = pd.read_parquet(config.ROOT / "data" / "extract" / "day_trading.parquet")
    retenues, validation = phase_a(extrait)
    final = phase_b(extrait, retenues)
    RESULTATS.write_text(json.dumps({
        "regle_de_decision": "piste retenue si gain d'accuracy selective a 2 % ET a 5 % en validation",
        "phase_a_validation": validation,
        "pistes_retenues": sorted(retenues),
        "phase_b_bougies_recentes": final,
    }, indent=2), encoding="utf-8")
    log.info("Resultats ecrits : %s (%.0f s)", RESULTATS, time.time() - debut)


if __name__ == "__main__":
    main()
