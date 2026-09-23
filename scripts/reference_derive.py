"""Photographie des donnees d'entrainement, pour pouvoir mesurer la derive.

Mesurer une derive suppose un point de comparaison. Ce script enregistre, pour
chaque variable, la repartition de ses valeurs pendant l'apprentissage du
modele : les bornes des 10 tranches de taille egale (deciles), et la part de
bougies dans chacune.

Plus tard, l'API compare les donnees recentes a cette photographie
(api/derive.py). La reference doit donc etre regeneree a CHAQUE
reentrainement, sinon on comparerait a un modele qui n'existe plus.

Usage :
    python -m scripts.reference_derive
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src import config
from scripts.pistes_amelioration import colonnes_de, construire, construire_colonnes_seules

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reference")

SORTIE = config.ROOT / "models" / "reference_derive.json"
TRANCHES = 10
# Le modele apprend sur les 72 premiers pourcents de l'extrait : la reference
# doit couvrir exactement cette periode, pas l'extrait entier.
PART_APPRENTISSAGE = 0.72


def photographier(donnees: pd.DataFrame, colonnes: list[str]) -> dict:
    """Deciles de chaque variable : 9 bornes decoupent 10 tranches egales."""
    photo = {}
    for nom in colonnes:
        valeurs = pd.to_numeric(donnees[nom], errors="coerce").dropna()
        if valeurs.empty:
            continue
        bornes = [float(q) for q in
                  np.unique(np.quantile(valeurs, np.linspace(0, 1, TRANCHES + 1)[1:-1]))]
        if not bornes:
            continue
        comptes = pd.cut(valeurs, bins=[-np.inf, *bornes, np.inf]).value_counts(sort=False)
        photo[nom] = {"bornes": bornes,
                      "proportions": [round(float(x), 6) for x in (comptes / comptes.sum())]}
    return photo


def construire_reference(apprentissage: pd.DataFrame, colonnes: list[str],
                         extrait_sha256: str) -> dict:
    """La photographie complete : toutes paires confondues, et par groupe.

    Utilisee par main() pour le modele de l'etape 3, et par le
    reentrainement automatique (scripts/reentrainer.py) pour chaque nouveau
    modele promu : la reference doit toujours decrire les donnees du modele
    EN SERVICE, sinon on mesurerait l'ecart a un modele qui n'existe plus.
    """
    # UNE REFERENCE PAR (PAIRE, PAS DE TEMPS).
    #
    # Deux pieges rencontres en construisant cette mesure, tous deux corriges ici :
    #
    #   1. la paire. La taille moyenne d'un trade vaut 0,005 sur le BTC et
    #      115,8 sur le XRP. Comparer le BTC a une reference melangeant les
    #      cinq paires fait crier a la derive alors que rien n'a bouge.
    #
    #   2. le pas de temps. L'ATR d'une bougie 4h vaut environ quatre fois
    #      celui d'une bougie 15m. L'entrainement contient 70 % de bougies
    #      15m ; si la mesure en contient un tiers, la simple difference de
    #      melange produit une derive fictive.
    #
    # La regle generale : on ne compare que des choses comparables.
    par_groupe = {f"{symbole}|{pas}": photographier(groupe, colonnes)
                  for (symbole, pas), groupe in apprentissage.groupby(["symbol", "interval"])}
    return {
        "periode": [str(apprentissage["open_time"].min()), str(apprentissage["open_time"].max())],
        "lignes": len(apprentissage),
        "tranches": TRANCHES,
        "extrait_sha256": extrait_sha256,
        "variables": photographier(apprentissage, colonnes),
        "par_groupe": par_groupe,
    }


def colonnes_surveillees(jeu: pd.DataFrame) -> list[str]:
    return [c for c in colonnes_de(jeu) if c not in {"rendement_suivant", "label"}]


def main():
    extrait = pd.read_parquet(config.ROOT / "data" / "extract" / "day_trading.parquet")
    jeu = construire_colonnes_seules(construire(extrait, set()), set())
    apprentissage = jeu.iloc[:int(len(jeu) * PART_APPRENTISSAGE)]

    manifeste = json.loads(
        (config.ROOT / "data" / "extract" / "manifest.json").read_text(encoding="utf-8"))
    reference = construire_reference(apprentissage, colonnes_surveillees(jeu),
                                     manifeste["profils"]["day_trading"]["sha256"])
    SORTIE.parent.mkdir(exist_ok=True)
    SORTIE.write_text(json.dumps(reference, indent=2), encoding="utf-8")
    log.info("Reference ecrite : %s (%d variables, %d groupes paire/pas, %d lignes, %s -> %s)",
             SORTIE, len(reference["variables"]), len(reference["par_groupe"]),
             len(apprentissage), reference["periode"][0][:10], reference["periode"][1][:10])


if __name__ == "__main__":
    main()
