"""Predire aussi la TAILLE du mouvement rendrait-il le modele rentable ?

L'idee : ne trader que les bougies qui vont beaucoup bouger, pour que le gain
depasse enfin les 0,2 % de frais.

Le gain brut moyen d'un modele juste dans une proportion `a`, sur des bougies
dont le mouvement moyen est `m`, vaut :

        (2a - 1) x m

Avec a = 0,57, il faut donc m > 1,43 % pour couvrir 0,2 % de frais, alors
qu'une bougie 15m bouge en moyenne de 0,227 %. La question devient : existe-t-il
assez de bougies aussi agitees, et le modele y voit-il encore clair ?

DEUX MESURES
    1. TAILLE CONNUE D'AVANCE (oracle). On classe les bougies par leur
       mouvement REEL - impossible en pratique, c'est le PLAFOND de ce qu'un
       predicteur de taille parfait pourrait apporter. Si meme la, ce n'est pas
       rentable, inutile d'entrainer quoi que ce soit.
    2. TAILLE PREDITE, avec ce qu'on a deja : l'ATR relatif, un indicateur de
       volatilite recente deja dans le modele et connu au moment de decider.

Usage :
    python -m scripts.taille_du_mouvement
"""
from __future__ import annotations

import json
import logging
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import joblib
import numpy as np
import pandas as pd

from src import config
from scripts.pistes_amelioration import RECENTES, colonnes_de, construire, construire_colonnes_seules
from scripts.profils_de_risque import wilson

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("taille")

FRAIS = 0.002
MODELE = config.ROOT / "models" / "direction_day_trading.joblib"
RESULTATS = config.DOCS / "taille_du_mouvement.json"


def par_tranche(jeu: pd.DataFrame, colonne: str, nom: str, tranches: int = 10) -> pd.DataFrame:
    """Decoupe les bougies en tranches selon `colonne` et mesure chaque tranche."""
    jeu = jeu.copy()
    jeu["tranche"] = pd.qcut(jeu[colonne], tranches, labels=False, duplicates="drop")
    lignes = []
    for tranche, g in jeu.groupby("tranche"):
        justes = int((g["prediction"] == g["label"]).sum())
        bas, haut = wilson(justes, len(g))
        brut = (g["rendement_suivant"] * np.where(g["prediction"] == 1, 1.0, -1.0)).mean()
        lignes.append({
            nom: f"{int(tranche) + 1}/10",
            "bougies": len(g),
            "mouvement moyen (%)": round(float(g["rendement_suivant"].abs().mean() * 100), 3),
            "bonnes reponses": round(justes / len(g), 4),
            "marge d'erreur": f"{bas:.3f}-{haut:.3f}",
            "gain brut (%)": round(float(brut * 100), 4),
            "gain net (%)": round(float((brut - FRAIS) * 100), 4),
        })
    return pd.DataFrame(lignes).set_index(nom)


def preparer_periode(paquet, jeu: pd.DataFrame) -> pd.DataFrame:
    proba = paquet["modele"].predict_proba(jeu[paquet["colonnes"]])[:, 1]
    jeu = jeu.copy()
    jeu["proba"] = proba
    jeu["prediction"] = (proba >= 0.5).astype(int)
    jeu["confiance"] = np.abs(proba - 0.5)
    return jeu


def main():
    paquet = joblib.load(MODELE)
    extrait = pd.read_parquet(config.ROOT / "data" / "extract" / "day_trading.parquet")
    jeu = construire(extrait, set())
    jeu = construire_colonnes_seules(jeu, set())
    periode = preparer_periode(paquet, jeu.iloc[int(len(jeu) * 0.80):])

    recentes = construire(pd.read_parquet(RECENTES), set())
    recentes = recentes[recentes["open_time"] > extrait["open_time"].max()]
    recentes = preparer_periode(paquet, construire_colonnes_seules(recentes, set()))

    sortie = {}
    for nom, donnees in (("test historique (90 765 bougies)", periode),
                         ("bougies jamais vues (14 831)", recentes)):
        log.info("")
        log.info("=== %s ===", nom)

        log.info("1. TAILLE CONNUE D'AVANCE (plafond theorique, impossible en pratique)")
        donnees = donnees.assign(mouvement_reel=donnees["rendement_suivant"].abs())
        oracle = par_tranche(donnees, "mouvement_reel", "tranche de mouvement")
        print(oracle.to_string())

        log.info("2. TAILLE PREDITE PAR L'ATR (realiste : connu au moment de decider)")
        atr = par_tranche(donnees, "atr_relatif", "tranche d'ATR")
        print(atr.to_string())

        # Combinaison : bougies les plus agitees ET modele le plus sur de lui.
        seuil = paquet["styles"]["agressif"] - 0.5
        agitees = donnees[donnees["atr_relatif"] >= donnees["atr_relatif"].quantile(0.9)]
        combine = agitees[agitees["confiance"] >= seuil]
        if len(combine):
            justes = int((combine["prediction"] == combine["label"]).sum())
            bas, haut = wilson(justes, len(combine))
            brut = float((combine["rendement_suivant"] *
                          np.where(combine["prediction"] == 1, 1.0, -1.0)).mean())
            log.info("3. LES DEUX : 10 %% de bougies les plus agitees ET modele sur de lui")
            log.info("   %d ordres | mouvement moyen %.3f %% | bonnes reponses %.4f [%.3f-%.3f] | "
                     "brut %+.4f %% | net %+.4f %%",
                     len(combine), combine["rendement_suivant"].abs().mean() * 100,
                     justes / len(combine), bas, haut, brut * 100, (brut - FRAIS) * 100)
            sortie[nom] = {"oracle": oracle.reset_index().to_dict("records"),
                           "atr": atr.reset_index().to_dict("records"),
                           "agitees_et_sur": {"ordres": len(combine),
                                              "bonnes_reponses": round(justes / len(combine), 4),
                                              "gain_brut_pct": round(brut * 100, 4),
                                              "gain_net_pct": round((brut - FRAIS) * 100, 4)}}

    RESULTATS.write_text(json.dumps(sortie, indent=2, default=str), encoding="utf-8")
    log.info("Resultats ecrits : %s", RESULTATS)


if __name__ == "__main__":
    main()
