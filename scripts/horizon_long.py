"""Predire le sens sur 4 heures ou 1 jour, et ne trader que le 1h et le 4h.

POURQUOI
    Sur une bougie 15m, le prix bouge en moyenne de 0,227 %, pour 0,2 % de
    frais par aller-retour : meme un modele PARFAIT ne gagnerait que 0,027 %
    par ordre. Il faudrait 94 % de bonnes reponses pour couvrir les frais.
    Sur 4 heures, le mouvement moyen est de 0,90 % (61 % suffisent) ; sur un
    jour, 2,36 % (54 % suffisent). Les frais sont fixes, le mouvement grandit.

CIBLE
    Pour une bougie cloturee a l'instant t : le prix sera-t-il plus haut a
    t + 4 h (ou t + 1 jour) qu'a t ?

    Seules les bougies 1h et 4h servent a apprendre et a trader. Le 15m
    reste une source d'information via le contexte, pas une occasion d'ordre.

PIEGE : LE CHEVAUCHEMENT DES ETIQUETTES
    Avec un horizon d'un jour, l'etiquette des dernieres lignes
    d'entrainement depend de prix situes DANS la periode suivante. Sans
    precaution, le modele apprend un morceau du futur de la validation.
    Correction ("purge", Lopez de Prado) : on retire de chaque periode les
    lignes dont l'etiquette deborde sur la periode suivante.

PROTOCOLE (identique aux scripts precedents)
    historique : modele 0-56 %, calibration 56-64 %, seuils 64-80 %, test 80-100 %
    recente    : modele 0-72 %, calibration 72-80 %, seuils 80-100 %, test sur
                 les bougies posterieures a l'extrait fige, jamais vues.
    Styles : agressif = 10 % des bougies de la periode des seuils, conservateur = 2 %.

BACKTEST
    10 000 EUR. Une seule position ouverte par paire : tant qu'elle court, les
    nouveaux signaux de cette paire sont ignores. 20 % du capital par
    position, donc au plus 5 x 20 % = 100 % engages. Entree et sortie aux
    prix de cloture, sans glissement de prix.

Usage :
    python -m scripts.horizon_long
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

from src import config
from src.preprocessing import INTERVAL_SECONDS
from scripts.direction_prochaine_bougie import preparer_depuis
from scripts.pistes_amelioration import RECENTES, colonnes_de, entrainer
from scripts.profils_de_risque import wilson

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("horizon")

RESULTATS = config.DOCS / "horizon_long.json"
HORIZONS = {"4h": pd.Timedelta(hours=4), "1j": pd.Timedelta(days=1)}
PAS_TRADES = ("1h", "4h")
ENSEMBLES = {"1h_et_4h": ("1h", "4h"), "4h_seul": ("4h",)}
STYLES = {"agressif": 0.10, "conservateur": 0.02}
MODES = ("acheteur_vendeur", "acheteur_seul")
FRAIS = {"standard_0.10%": 0.001, "bnb_0.075%": 0.00075, "sans_frais": 0.0}
CAPITAL, FRACTION = 10_000.0, 0.20


# ---------------------------------------------------------------------------
#  Donnees
# ---------------------------------------------------------------------------

def construire(brut: pd.DataFrame, horizon: pd.Timedelta) -> pd.DataFrame:
    """Variables (avec contexte) + sens du prix a l'horizon, pour le 1h et le 4h."""
    jeu = preparer_depuis(brut, contexte=True)
    jeu = jeu[jeu["interval"].isin(PAS_TRADES)].drop(columns=["label", "rendement_suivant"])

    morceaux = []
    for (symbole, pas), g in brut[brut["interval"].isin(PAS_TRADES)].groupby(["symbol", "interval"]):
        g = g.sort_values("open_time")
        k = int(horizon.total_seconds() // INTERVAL_SECONDS[pas])
        morceaux.append(pd.DataFrame({
            "symbol": symbole, "interval": pas, "open_time": g["open_time"].to_numpy(),
            # Du prix de cloture de la bougie au prix de cloture k bougies plus tard.
            "rendement_horizon": (g["close"].shift(-k) / g["close"] - 1).to_numpy(),
        }))
    cible = pd.concat(morceaux, ignore_index=True)

    jeu = jeu.merge(cible, on=["symbol", "interval", "open_time"], how="inner")
    jeu = jeu.dropna(subset=["rendement_horizon"])
    jeu = jeu[jeu["rendement_horizon"] != 0]
    jeu["label"] = (jeu["rendement_horizon"] > 0).astype(int)

    duree = pd.to_timedelta(jeu["interval"].map(INTERVAL_SECONDS), unit="s")
    jeu["entree"] = jeu["open_time"] + duree            # cloture de la bougie du signal
    jeu["sortie"] = jeu["entree"] + horizon             # fin de l'etiquette
    return jeu.sort_values("open_time").reset_index(drop=True)


def decouper(jeu: pd.DataFrame, bornes: list[float]) -> list[pd.DataFrame]:
    """Periodes chronologiques PURGEES : aucune etiquette ne deborde sur la suivante."""
    n = len(jeu)
    indices = [int(n * b) for b in bornes]
    periodes = []
    for debut, fin in zip(indices[:-1], indices[1:]):
        periode = jeu.iloc[debut:fin]
        if fin < n:
            debut_suivante = jeu.iloc[fin]["open_time"]
            avant = len(periode)
            periode = periode[periode["sortie"] <= debut_suivante]
            periode.attrs["purgees"] = avant - len(periode)
        periodes.append(periode)
    return periodes


# ---------------------------------------------------------------------------
#  Mesures
# ---------------------------------------------------------------------------

def signaux(modele, periode: pd.DataFrame, colonnes, seuil: float, pas: tuple) -> pd.DataFrame:
    proba = modele.predict_proba(periode[colonnes])[:, 1]
    garde = (np.abs(proba - 0.5) >= seuil) & periode["interval"].isin(pas).to_numpy()
    choisis = periode.loc[garde, ["symbol", "interval", "entree", "sortie",
                                  "rendement_horizon", "label"]].copy()
    choisis["sens"] = np.where(proba[garde] >= 0.5, 1.0, -1.0)
    return choisis.sort_values("entree")


def une_position_par_paire(ordres: pd.DataFrame) -> pd.DataFrame:
    """Ignore les signaux d'une paire tant que sa position precedente est ouverte."""
    gardes, occupee_jusqu_a = [], {}
    for i, o in zip(ordres.index, ordres.itertuples()):
        if o.entree >= occupee_jusqu_a.get(o.symbol, pd.Timestamp.min.tz_localize("UTC")):
            gardes.append(i)
            occupee_jusqu_a[o.symbol] = o.sortie
    return ordres.loc[gardes]


def simuler(ordres: pd.DataFrame, frais: float) -> dict:
    if ordres.empty:
        return {"ordres": 0}
    ordres = ordres.sort_values("sortie")
    capital, courbe, nets = CAPITAL, [CAPITAL], []
    for sens, r in zip(ordres["sens"].to_numpy(), ordres["rendement_horizon"].to_numpy()):
        net = (1 + sens * r) * (1 - frais) ** 2 - 1
        capital += capital * FRACTION * net
        courbe.append(capital)
        nets.append(net)
    courbe, nets = np.array(courbe), np.array(nets)
    sommets = np.maximum.accumulate(courbe)
    return {
        "ordres": len(ordres),
        "capital_final": round(float(capital), 2),
        "performance_pct": round(float((capital / CAPITAL - 1) * 100), 2),
        "pire_baisse_pct": round(float(((courbe - sommets) / sommets).min() * 100), 2),
        "gain_moyen_net_par_ordre_pct": round(float(nets.mean() * 100), 4),
    }


def conserver(brut: pd.DataFrame, debut, fin) -> float:
    b = brut[(brut["interval"] == "1h") & (brut["open_time"] >= debut) & (brut["open_time"] <= fin)]
    r = b.sort_values("open_time").groupby("symbol")["close"].agg(lambda c: c.iloc[-1] / c.iloc[0] - 1)
    return round(float(r.mean() * 100), 2)


def evaluer(nom, modele, colonnes, seuils_sur, test, brut) -> dict:
    jours = (test["open_time"].max() - test["open_time"].min()).total_seconds() / 86400
    confiance = np.abs(modele.predict_proba(seuils_sur[colonnes])[:, 1] - 0.5)
    reference = conserver(brut, test["open_time"].min(), test["open_time"].max())
    log.info("  --- %s : %.1f jours | acheter et conserver : %+.2f %% ---", nom, jours, reference)

    sortie = {"jours": round(jours, 1), "acheter_et_conserver_pct": reference, "resultats": {}}
    for ensemble, pas in ENSEMBLES.items():
        sous_test = test[test["interval"].isin(pas)]
        mouvement = float(sous_test["rendement_horizon"].abs().mean())
        seuil_rentable = 0.5 + 0.002 / (2 * mouvement)
        for style, niveau in STYLES.items():
            seuil = float(np.quantile(confiance, 1 - niveau))
            tous = signaux(modele, test, colonnes, seuil, pas)
            cle = f"{ensemble}/{style}"
            if tous.empty:
                sortie["resultats"][cle] = {"signaux": 0}
                log.info("  %-24s aucun signal", cle)
                continue
            justes = int((((tous["sens"] > 0).astype(int)) == tous["label"]).sum())
            bas, haut = wilson(justes, len(tous))
            ligne = {
                "seuil_probabilite": round(0.5 + seuil, 4),
                "signaux": len(tous),
                "accuracy": round(justes / len(tous), 4),
                "ic95": [round(bas, 4), round(haut, 4)],
                "part_hausse_pct": round(float((tous["sens"] > 0).mean() * 100), 1),
                "mouvement_moyen_pct": round(mouvement * 100, 3),
                "accuracy_necessaire_frais_0.10%": round(seuil_rentable, 4),
                "backtest": {},
            }
            for mode in MODES:
                ordres = tous if mode == "acheteur_vendeur" else tous[tous["sens"] > 0]
                ordres = une_position_par_paire(ordres)
                ligne["backtest"][mode] = {f: simuler(ordres, v) for f, v in FRAIS.items()}
            sortie["resultats"][cle] = ligne

            av = ligne["backtest"]["acheteur_vendeur"]
            aseul = ligne["backtest"]["acheteur_seul"]
            log.info("  %-24s p>=%.3f %5d signaux acc %.4f [%.3f-%.3f] (besoin %.3f) hausse %4.1f%% | "
                     "A+V : %4d ordres %+7.2f %% (sans frais %+7.2f %%) | A seul : %4s ordres %s",
                     cle, ligne["seuil_probabilite"], len(tous), ligne["accuracy"], bas, haut,
                     seuil_rentable, ligne["part_hausse_pct"],
                     av["standard_0.10%"]["ordres"], av["standard_0.10%"].get("performance_pct", 0),
                     av["sans_frais"].get("performance_pct", 0),
                     aseul["standard_0.10%"]["ordres"],
                     f"{aseul['standard_0.10%'].get('performance_pct', 0):+.2f} %")
    return sortie


def main():
    debut = time.time()
    extrait = pd.read_parquet(config.ROOT / "data" / "extract" / "day_trading.parquet")
    recentes_brutes = pd.read_parquet(RECENTES)
    fin_extrait = extrait["open_time"].max()
    resultats = {}

    for nom_horizon, horizon in HORIZONS.items():
        log.info("=== HORIZON %s ===", nom_horizon)
        jeu = construire(extrait, horizon)
        colonnes = [c for c in colonnes_de(jeu) if c not in {"rendement_horizon", "entree", "sortie"}]
        resultats[nom_horizon] = {}

        # Historique
        train, calib, seuils, test = decouper(jeu, [0, 0.56, 0.64, 0.80, 1.0])
        log.info("  lignes purgees aux frontieres : %s",
                 [p.attrs.get("purgees", 0) for p in (train, calib, seuils)])
        modele = entrainer(train[colonnes], train["label"], calib[colonnes], calib["label"])
        resultats[nom_horizon]["historique"] = evaluer(
            "historique (test 80-100 %)", modele, colonnes, seuils, test, extrait)

        # Recente, jamais vue
        train, calib, seuils = decouper(jeu, [0, 0.72, 0.80, 1.0])
        modele = entrainer(train[colonnes], train["label"], calib[colonnes], calib["label"])
        recentes = construire(recentes_brutes, horizon)
        recentes = recentes[recentes["open_time"] > fin_extrait].reset_index(drop=True)
        resultats[nom_horizon]["recente"] = evaluer(
            "recente, jamais vue", modele, colonnes, seuils, recentes, recentes_brutes)

    RESULTATS.write_text(json.dumps({
        "capital_initial": CAPITAL, "fraction_par_position": FRACTION,
        "regle": "une position ouverte par paire, entree et sortie aux clotures, sans glissement",
        "horizons": resultats,
    }, indent=2, default=str), encoding="utf-8")
    log.info("Resultats ecrits : %s (%.0f s)", RESULTATS, time.time() - debut)


if __name__ == "__main__":
    main()
