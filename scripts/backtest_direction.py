"""Backtest en euros du modele "sens de la prochaine bougie" (version calibree).

Question : avec 10 000 EUR, qu'aurait donne le bot ?

REGLES DE SIMULATION
    - Un ordre est ouvert a la CLOTURE de la bougie ou le modele se prononce,
      et ferme a la cloture de la bougie suivante. C'est exactement ce que
      mesure la cible : aucun prix n'est invente.
    - Chaque ordre engage 5 % du capital du moment. Avec 5 paires et 3 pas de
      temps, au plus 15 ordres peuvent etre ouverts ensemble : 15 x 5 % =
      75 %, on n'engage donc jamais d'argent qu'on n'a pas. L'exposition
      maximale reellement atteinte est mesuree et affichee.
    - Frais preleves a l'entree ET a la sortie.
    - Pas de glissement de prix (slippage) : on suppose l'execution au prix
      de cloture. C'est optimiste, surtout pour 50 ordres par jour.

DEUX MODES
    acheteur_seul       le seul possible sur le marche SPOT : quand le modele
                        prevoit une baisse, on ne fait rien.
    acheteur_vendeur    on parie aussi a la baisse ; necessite les futures
                        (ou la marge), avec leurs propres frais et risques.

TROIS NIVEAUX DE FRAIS
    0,10 %   tarif standard Binance spot
    0,075 %  avec paiement des frais en BNB (-25 %)
    0 %      irrealiste, mais mesure l'avantage BRUT du modele

DEUX PERIODES (memes protocoles que scripts/pistes_amelioration.py)
    historique   mars -> aout 2026. Modele 0-56 %, calibration 56-64 %,
                 seuils 64-80 %, backtest 80-100 %.
    recente      24 aout -> 16 septembre, jamais vue. Modele 0-72 %,
                 calibration 72-80 %, seuils 80-100 %.

Usage :
    python -m scripts.backtest_direction
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
from scripts.pistes_amelioration import (
    RECENTES, STYLES, colonnes_de, construire, construire_colonnes_seules, entrainer,
)

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest")

RESULTATS = config.DOCS / "backtest_direction.json"
CAPITAL = 10_000.0
FRACTION = 0.05
FRAIS = {"standard_0.10%": 0.001, "bnb_0.075%": 0.00075, "sans_frais": 0.0}
MODES = ("acheteur_seul", "acheteur_vendeur")


def exposition_maximale(entrees: np.ndarray, sorties: np.ndarray) -> int:
    """Nombre maximal d'ordres ouverts en meme temps (balayage des evenements)."""
    evenements = sorted([(t, 1) for t in entrees] + [(t, -1) for t in sorties],
                        key=lambda e: (e[0], e[1]))  # une sortie passe avant une entree simultanee
    ouverts = maximum = 0
    for _, delta in evenements:
        ouverts += delta
        maximum = max(maximum, ouverts)
    return maximum


def simuler(ordres: pd.DataFrame, frais: float) -> dict:
    """Rejoue les ordres dans l'ordre de leur CLOTURE, en capital compose."""
    if ordres.empty:
        return {"ordres": 0, "capital_final": CAPITAL, "performance_pct": 0.0}
    ordres = ordres.sort_values("sortie")
    capital, courbe = CAPITAL, [CAPITAL]
    nets = []
    for sens, rendement in zip(ordres["sens"].to_numpy(), ordres["rendement"].to_numpy()):
        net = (1 + sens * rendement) * (1 - frais) ** 2 - 1
        capital += capital * FRACTION * net
        courbe.append(capital)
        nets.append(net)

    courbe = np.array(courbe)
    perte_max = ((courbe - np.maximum.accumulate(courbe)) / np.maximum.accumulate(courbe)).min()
    nets = np.array(nets)
    return {
        "ordres": len(ordres),
        "capital_final": round(float(capital), 2),
        "performance_pct": round(float((capital / CAPITAL - 1) * 100), 2),
        "perte_max_pct": round(float(perte_max * 100), 2),
        "ordres_gagnants_pct": round(float((nets > 0).mean() * 100), 2),
        "gain_moyen_net_par_ordre_pct": round(float(nets.mean() * 100), 4),
        "frais_payes_eur_approx": round(float(len(ordres) * 2 * frais * CAPITAL * FRACTION), 0),
    }


def construire_ordres(modele, periode: pd.DataFrame, colonnes, seuil_confiance: float,
                      mode: str) -> pd.DataFrame:
    proba = modele.predict_proba(periode[colonnes])[:, 1]
    garde = np.abs(proba - 0.5) >= seuil_confiance
    if mode == "acheteur_seul":
        garde &= proba >= 0.5
    choisis = periode.loc[garde]
    duree = pd.to_timedelta(choisis["interval"].map(INTERVAL_SECONDS), unit="s")
    return pd.DataFrame({
        "sens": np.where(proba[garde] >= 0.5, 1.0, -1.0),
        "rendement": choisis["rendement_suivant"].to_numpy(),
        # Entree a la cloture de la bougie du signal, sortie une bougie plus tard.
        "entree": (choisis["open_time"] + duree).to_numpy(),
        "sortie": (choisis["open_time"] + 2 * duree).to_numpy(),
    })


def conserver(periode_brute: pd.DataFrame, debut, fin) -> float:
    """Reference : acheter les 5 paires a parts egales et ne rien faire."""
    bougies = periode_brute[(periode_brute["interval"] == "1h")
                            & (periode_brute["open_time"] >= debut)
                            & (periode_brute["open_time"] <= fin)]
    rendements = bougies.sort_values("open_time").groupby("symbol")["close"].agg(
        lambda c: c.iloc[-1] / c.iloc[0] - 1)
    return round(float(rendements.mean() * 100), 2)


def backtester(nom: str, modele, colonnes, seuils_sur: pd.DataFrame, periode: pd.DataFrame,
               brut: pd.DataFrame) -> dict:
    jours = (periode["open_time"].max() - periode["open_time"].min()).total_seconds() / 86400
    confiance = np.abs(modele.predict_proba(seuils_sur[colonnes])[:, 1] - 0.5)
    reference = conserver(brut, periode["open_time"].min(), periode["open_time"].max())
    log.info("=== %s : %.1f jours | acheter et conserver les 5 paires : %+.2f %% ===",
             nom, jours, reference)

    sortie = {"jours": round(jours, 1), "acheter_et_conserver_pct": reference, "styles": {}}
    for style, niveau in STYLES.items():
        seuil = float(np.quantile(confiance, 1 - niveau))
        sortie["styles"][style] = {"seuil_probabilite": round(0.5 + seuil, 4), "modes": {}}
        for mode in MODES:
            ordres = construire_ordres(modele, periode, colonnes, seuil, mode)
            expo = exposition_maximale(ordres["entree"].to_numpy(), ordres["sortie"].to_numpy()) \
                if len(ordres) else 0
            resultats = {cle: simuler(ordres, f) for cle, f in FRAIS.items()}
            sortie["styles"][style]["modes"][mode] = {
                "exposition_maximale_pct": expo * FRACTION * 100, "frais": resultats}
            for cle, r in resultats.items():
                if not r["ordres"]:
                    continue
                log.info("  %-12s %-17s %-15s %5d ordres | %9.2f EUR (%+7.2f %%) | pire baisse %6.2f %% | expo max %3.0f %%",
                         style, mode, cle, r["ordres"], r["capital_final"], r["performance_pct"],
                         r["perte_max_pct"], expo * FRACTION * 100)
    return sortie


def main():
    debut = time.time()
    extrait = pd.read_parquet(config.ROOT / "data" / "extract" / "day_trading.parquet")
    recentes_brutes = pd.read_parquet(RECENTES)

    jeu = construire(extrait, set())
    colonnes = colonnes_de(construire_colonnes_seules(jeu, set()))
    n = len(jeu)
    i56, i64, i72, i80 = (int(n * p) for p in (0.56, 0.64, 0.72, 0.80))

    # Periode historique
    modele = entrainer(jeu.iloc[:i56][colonnes], jeu.iloc[:i56]["label"],
                       jeu.iloc[i56:i64][colonnes], jeu.iloc[i56:i64]["label"])
    historique = backtester("HISTORIQUE (mars -> aout 2026)", modele, colonnes,
                            jeu.iloc[i64:i80], jeu.iloc[i80:].reset_index(drop=True), extrait)

    # Periode recente, jamais vue
    modele = entrainer(jeu.iloc[:i72][colonnes], jeu.iloc[:i72]["label"],
                       jeu.iloc[i72:i80][colonnes], jeu.iloc[i72:i80]["label"])
    recentes = construire(recentes_brutes, set())
    recentes = recentes[recentes["open_time"] > extrait["open_time"].max()].reset_index(drop=True)
    recente = backtester("RECENTE, JAMAIS VUE (24 aout -> 16 sept.)", modele, colonnes,
                         jeu.iloc[i80:], recentes, recentes_brutes)

    RESULTATS.write_text(json.dumps({
        "capital_initial": CAPITAL, "fraction_par_ordre": FRACTION,
        "hypotheses": "entree et sortie aux prix de cloture, sans glissement de prix",
        "historique": historique, "recente": recente,
    }, indent=2, default=str), encoding="utf-8")
    log.info("Resultats ecrits : %s (%.0f s)", RESULTATS, time.time() - debut)


if __name__ == "__main__":
    main()
