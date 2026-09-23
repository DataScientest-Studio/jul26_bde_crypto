"""Nos stop loss et take profit sont-ils bien places ?

L'OUTIL STANDARD : MAE ET MFE
    Pour chaque trade, on regarde le chemin parcouru par le prix pendant qu'il
    etait ouvert :

      MAE  (maximum adverse excursion)    jusqu'ou le prix est alle CONTRE nous
      MFE  (maximum favorable excursion)  jusqu'ou il est alle EN NOTRE FAVEUR

    Les deux se lisent ensemble et repondent a deux questions concretes :

      - le stop est-il trop serre ? Si les trades GAGNANTS ont souvent une MAE
        proche du stop, on se fait sortir juste avant que ca reparte.
      - l'objectif est-il trop proche ? Si les trades gagnants ont une MFE bien
        au-dela du take profit, on laisse de l'argent sur la table.

    Les distances sont exprimees en SIGMA (l'ecart-type des rendements recents)
    et non en pourcentage : c'est la seule facon de comparer une bougie calme
    et une bougie agitee, ou le BTC et le XRP.

LE RATIO D'AVANTAGE (E-ratio)
    moyenne(MFE) / moyenne(MAE), a horizon fixe. Il mesure la qualite du SIGNAL
    D'ENTREE, independamment des barrieres : au-dessus de 1, le prix va plus
    loin en notre faveur que contre nous. C'est la mesure des Turtles.

COMPARAISON DE CONFIGURATIONS
    On rejoue les MEMES signaux avec differentes largeurs de barrieres, y
    compris asymetriques (objectif plus loin que le stop), et on compare le
    gain moyen NET de frais. C'est ce qui dit si notre 3 sigma / 3 sigma est un
    bon choix.

Usage :
    python -m scripts.qualite_barrieres
    python -m scripts.qualite_barrieres --pairs BTCUSDT --intervalles 1h 4h
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src import config
from src.labeling import volatilite_glissante
from api import donnees, modele
from api.ordre import FENETRE_VOLATILITE, charger_barrieres

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("barrieres")

RESULTATS = config.DOCS / "qualite_barrieres.json"
FRAIS = 0.002

# Configurations comparees : (take profit, stop loss) en sigma.
CONFIGURATIONS = [
    (1.0, 1.0), (2.0, 2.0), (3.0, 3.0), (4.0, 4.0),     # symetriques
    (3.0, 1.5), (4.0, 2.0), (6.0, 3.0),                 # objectif 2x le risque
    (1.5, 3.0), (2.0, 4.0),                             # l'inverse, pour verifier
]


def chemins(serie: pd.DataFrame, signaux: list[dict], horizon: int,
            volatilite: np.ndarray) -> list[dict]:
    """Pour chaque signal : le chemin du prix, en sigma, sur l'horizon."""
    hauts, bas, clotures = (serie["high"].to_numpy(), serie["low"].to_numpy(),
                            serie["close"].to_numpy())
    sortie = []
    for signal in signaux:
        i = signal["indice"]
        if i + horizon >= len(serie) or not np.isfinite(volatilite[i]) or volatilite[i] <= 0:
            continue
        sens, entree, sigma = signal["sens"], clotures[i], volatilite[i]
        fenetre = slice(i + 1, i + 1 + horizon)

        # Chemin favorable et defavorable, bougie par bougie, en sigma.
        if sens == 1:
            favorable = (hauts[fenetre] / entree - 1) / sigma
            defavorable = (bas[fenetre] / entree - 1) / sigma
        else:
            favorable = -(bas[fenetre] / entree - 1) / sigma
            defavorable = -(hauts[fenetre] / entree - 1) / sigma

        sortie.append({
            "sens": sens, "entree": entree, "sigma": sigma,
            "favorable": favorable, "defavorable": defavorable,
            "mfe": float(np.max(favorable)), "mae": float(-np.min(defavorable)),
            "rendement_fin": float((clotures[i + horizon] / entree - 1) * sens / sigma),
        })
    return sortie


def rejouer_configuration(chemins_: list[dict], tp: float, sl: float) -> dict:
    """Que donnerait cette paire de barrieres sur les memes signaux ?"""
    resultats = []
    for chemin in chemins_:
        touche_gain = np.argmax(chemin["favorable"] >= tp) if (chemin["favorable"] >= tp).any() else None
        touche_stop = np.argmax(chemin["defavorable"] <= -sl) if (chemin["defavorable"] <= -sl).any() else None

        if touche_stop is not None and (touche_gain is None or touche_stop <= touche_gain):
            # Hypothese prudente : a egalite dans la meme bougie, la perte gagne.
            brut = -sl * chemin["sigma"]
            issue = "stop loss"
        elif touche_gain is not None:
            brut = tp * chemin["sigma"]
            issue = "take profit"
        else:
            brut = chemin["rendement_fin"] * chemin["sigma"]
            issue = "echeance"
        resultats.append({"brut": brut, "net": brut - FRAIS, "issue": issue})

    if not resultats:
        return {}
    nets = np.array([r["net"] for r in resultats])
    return {
        "take_profit_sigma": tp, "stop_loss_sigma": sl,
        "trades": len(resultats),
        "gagnants_pct": round(float((nets > 0).mean() * 100), 1),
        "gain_moyen_net_pct": round(float(nets.mean() * 100), 4),
        "issues": {issue: sum(1 for r in resultats if r["issue"] == issue)
                   for issue in ("take profit", "stop loss", "echeance")},
    }


def analyser(paire: str, interval: str, style: str, bougies: pd.DataFrame,
             horizon: int, largeur_actuelle: float) -> dict | None:
    serie = (bougies[(bougies["symbol"] == paire) & (bougies["interval"] == interval)]
             .sort_values("open_time").reset_index(drop=True))
    if len(serie) < 200:
        return None

    decisions = modele.predire_serie(bougies, paire, interval, style, limite=len(serie))
    position = {t.isoformat(): i for i, t in enumerate(serie["open_time"])}
    signaux = [{"indice": position[d["open_time"]],
                "sens": 1 if d["decision"] == "acheter" else -1}
               for d in decisions if d["decision"] != "attendre" and d["open_time"] in position]
    if len(signaux) < 10:
        return None

    volatilite = volatilite_glissante(serie["close"], FENETRE_VOLATILITE).to_numpy()
    chemins_ = chemins(serie, signaux, horizon, volatilite)
    if len(chemins_) < 10:
        return None

    mfe = np.array([c["mfe"] for c in chemins_])
    mae = np.array([c["mae"] for c in chemins_])

    # Avec les barrieres actuelles, qui gagne et qui perd ?
    actuelle = rejouer_configuration(chemins_, largeur_actuelle, largeur_actuelle)
    perdants = [c for c in chemins_ if c["mae"] >= largeur_actuelle
                and c["mfe"] < largeur_actuelle]
    gagnants = [c for c in chemins_ if c["mfe"] >= largeur_actuelle]

    return {
        "trades_analyses": len(chemins_),
        "mae_sigma": {"mediane": round(float(np.median(mae)), 2),
                      "moyenne": round(float(mae.mean()), 2),
                      "q90": round(float(np.quantile(mae, 0.9)), 2)},
        "mfe_sigma": {"mediane": round(float(np.median(mfe)), 2),
                      "moyenne": round(float(mfe.mean()), 2),
                      "q90": round(float(np.quantile(mfe, 0.9)), 2)},
        # Au-dessus de 1, le prix va plus loin en notre faveur que contre nous.
        "ratio_avantage": round(float(mfe.mean() / mae.mean()), 3) if mae.mean() else None,
        # Parmi les trades sortis en perte, combien allaient d'abord dans le bon
        # sens ? Une MFE elevee chez les perdants = stop trop serre ou objectif
        # trop loin.
        "mfe_moyenne_des_perdants_sigma": round(float(np.mean([c["mfe"] for c in perdants])), 2)
                                           if perdants else None,
        "mae_moyenne_des_gagnants_sigma": round(float(np.mean([c["mae"] for c in gagnants])), 2)
                                           if gagnants else None,
        "configuration_actuelle": actuelle,
        "configurations": [rejouer_configuration(chemins_, tp, sl) for tp, sl in CONFIGURATIONS],
    }


def main():
    parser = argparse.ArgumentParser(description="Qualite des barrieres")
    parser.add_argument("--pairs", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    parser.add_argument("--intervalles", nargs="+", default=["15m", "1h", "4h"])
    parser.add_argument("--style", default="conservateur")
    args = parser.parse_args()

    barrieres = charger_barrieres()
    largeur, horizon = float(barrieres["largeur_barrieres"]), int(barrieres["horizon"])
    log.info("Barrieres actuelles : %.0f sigma de chaque cote, echeance %d bougies",
             largeur, horizon)

    resultats, agreges = {}, []
    for paire in args.pairs:
        bougies = donnees.bougies_binance(paire, 1000)
        for interval in args.intervalles:
            analyse = analyser(paire, interval, args.style, bougies, horizon, largeur)
            if analyse is None:
                continue
            cle = f"{paire}|{interval}"
            resultats[cle] = analyse
            agreges.append(analyse)
            log.info("%-14s %3d trades | MAE med %.2f s | MFE med %.2f s | avantage %.2f | "
                     "actuel : %4.1f %% gagnants, %+.3f %%", cle, analyse["trades_analyses"],
                     analyse["mae_sigma"]["mediane"], analyse["mfe_sigma"]["mediane"],
                     analyse["ratio_avantage"] or 0,
                     analyse["configuration_actuelle"]["gagnants_pct"],
                     analyse["configuration_actuelle"]["gain_moyen_net_pct"])

    if not agreges:
        log.warning("Pas assez de signaux pour conclure.")
        return

    log.info("")
    log.info("COMPARAISON DES BARRIERES (moyenne sur %d jeux de donnees)", len(agreges))
    log.info("%-22s %8s %10s %12s", "take profit / stop", "gagnants", "gain net", "issues")
    classement = []
    for index, (tp, sl) in enumerate(CONFIGURATIONS):
        lignes = [a["configurations"][index] for a in agreges if a["configurations"][index]]
        gagnants = float(np.mean([l["gagnants_pct"] for l in lignes]))
        gain = float(np.mean([l["gain_moyen_net_pct"] for l in lignes]))
        issues = {cle: sum(l["issues"][cle] for l in lignes)
                  for cle in ("take profit", "stop loss", "echeance")}
        classement.append({"take_profit_sigma": tp, "stop_loss_sigma": sl,
                           "gagnants_pct": round(gagnants, 1),
                           "gain_moyen_net_pct": round(gain, 4), "issues": issues})
        marque = "  <- configuration actuelle" if (tp, sl) == (largeur, largeur) else ""
        log.info("%-22s %7.1f %% %+9.3f %% %12s%s", f"{tp:g} s / {sl:g} s", gagnants, gain,
                 f"{issues['take profit']}/{issues['stop loss']}/{issues['echeance']}", marque)

    meilleure = max(classement, key=lambda c: c["gain_moyen_net_pct"])
    log.info("")
    log.info("Meilleure configuration mesuree : %g sigma / %g sigma (%+.3f %% par trade)",
             meilleure["take_profit_sigma"], meilleure["stop_loss_sigma"],
             meilleure["gain_moyen_net_pct"])

    RESULTATS.write_text(json.dumps({
        "barrieres_actuelles": {"largeur_sigma": largeur, "horizon": horizon},
        "style": args.style, "frais_aller_retour": FRAIS,
        "par_jeu_de_donnees": resultats,
        "comparaison": classement,
        "meilleure_mesuree": meilleure,
    }, indent=2), encoding="utf-8")
    log.info("Resultats ecrits : %s", RESULTATS)


if __name__ == "__main__":
    main()
