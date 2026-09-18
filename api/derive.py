"""Mesure de la derive des donnees.

UNE DERIVE, C'EST QUOI ?
    Le modele a appris sur des donnees d'une certaine periode. Si le marche
    change - volatilite qui double, volumes qui s'effondrent, nouveau regime -
    les variables qu'il recoit ne ressemblent plus a celles de son
    apprentissage. Ses predictions deviennent alors douteuses, MEME si le code
    fonctionne parfaitement. Rien ne plante : c'est justement le danger.

COMMENT ON LA MESURE : L'INDICE PSI
    Pour chaque variable, on avait decoupe les valeurs d'entrainement en 10
    tranches de taille egale (les deciles). On regarde ou tombent les valeurs
    RECENTES dans ces memes tranches. Si la repartition est identique, chaque
    tranche recoit 10 % des bougies. Si tout s'entasse dans une tranche, la
    variable a derive.

        PSI = somme sur les tranches de (p_recent - p_reference)
                                        x ln(p_recent / p_reference)

    Seuils d'usage courant dans l'industrie :
        PSI < 0,10   stable
        0,10 - 0,25  derive moderee, a surveiller
        PSI > 0,25   derive forte, reentrainement conseille

    Le PSI est prefere au test de Kolmogorov-Smirnov ici : sur des dizaines de
    milliers de bougies, ce dernier declare TOUT significatif, y compris des
    ecarts sans consequence pratique.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

from src import config

logger = logging.getLogger(__name__)

CHEMIN_REFERENCE = Path(os.getenv("CRYPTOBOT_REFERENCE_DERIVE",
                                  config.ROOT / "models" / "reference_derive.json"))
SEUIL_MODERE, SEUIL_FORT = 0.10, 0.25
EPSILON = 1e-6  # evite un logarithme de zero quand une tranche se vide


class ReferenceIndisponible(RuntimeError):
    """Pas de reference : impossible de comparer quoi que ce soit."""


def charger_reference() -> dict:
    if not CHEMIN_REFERENCE.exists():
        raise ReferenceIndisponible(
            f"{CHEMIN_REFERENCE} introuvable. Lancer : python -m scripts.reference_derive"
        )
    return json.loads(CHEMIN_REFERENCE.read_text(encoding="utf-8"))


def psi(valeurs: pd.Series, bornes: list[float], proportions_reference: list[float]) -> float:
    """Indice de stabilite d'une variable entre l'entrainement et aujourd'hui."""
    valeurs = pd.to_numeric(valeurs, errors="coerce").dropna()
    if valeurs.empty:
        return float("nan")
    # -inf et +inf aux extremites : une valeur jamais vue a l'entrainement
    # tombe dans la premiere ou la derniere tranche au lieu d'etre ignoree.
    decoupage = [-np.inf, *bornes, np.inf]
    comptes = pd.cut(valeurs, bins=decoupage, duplicates="drop").value_counts(sort=False)
    proportions = (comptes / comptes.sum()).to_numpy()
    reference = np.asarray(proportions_reference, dtype=float)
    if len(proportions) != len(reference):
        return float("nan")
    p, r = proportions + EPSILON, reference + EPSILON
    return float(np.sum((p - r) * np.log(p / r)))


def qualifier(indice: float) -> str:
    if not np.isfinite(indice):
        return "inconnu"
    if indice < SEUIL_MODERE:
        return "stable"
    return "derive moderee" if indice < SEUIL_FORT else "derive forte"


def mesurer(variables: pd.DataFrame, reference: dict | None = None,
            symbole: str | None = None, interval: str | None = None) -> dict:
    """Compare les variables recentes a la reference d'entrainement.

    ON NE COMPARE QUE DES CHOSES COMPARABLES. La reference est choisie pour la
    paire ET le pas de temps mesures, car les deux changent completement les
    ordres de grandeur : la taille moyenne d'un trade va de 0,005 sur le BTC a
    115,8 sur le XRP, et l'ATR d'une bougie 4h vaut environ quatre fois celui
    d'une bougie 15m. Comparer a une reference melangee produirait une derive
    fictive - erreur commise puis corrigee pendant le developpement.
    """
    reference = reference or charger_reference()
    photo = reference["variables"]
    cle = f"{symbole}|{interval}"
    if cle in reference.get("par_groupe", {}):
        photo = reference["par_groupe"][cle]
        variables = variables[(variables["symbol"] == symbole)
                              & (variables["interval"] == interval)]
    lignes = []
    for nom, ref in photo.items():
        if nom not in variables.columns:
            continue
        indice = psi(variables[nom], ref["bornes"], ref["proportions"])
        lignes.append({"variable": nom, "psi": round(indice, 4) if np.isfinite(indice) else None,
                       "statut": qualifier(indice)})

    lignes.sort(key=lambda l: (l["psi"] is None, -(l["psi"] or 0)))
    valides = [l["psi"] for l in lignes if l["psi"] is not None]
    fortes = [l for l in lignes if l["statut"] == "derive forte"]
    moderees = [l for l in lignes if l["statut"] == "derive moderee"]

    if fortes:
        verdict = "derive forte : reentrainement conseille"
    elif len(moderees) >= 3:
        verdict = "derive moderee sur plusieurs variables : a surveiller"
    elif moderees:
        verdict = "derive moderee isolee : sans consequence immediate"
    else:
        verdict = "stable : les donnees ressemblent a celles de l'entrainement"

    return {
        "reference": {"periode": reference.get("periode"), "lignes": reference.get("lignes"),
                      "extrait_sha256": reference.get("extrait_sha256"),
                      "portee": cle if cle in reference.get("par_groupe", {}) else "toutes les paires"},
        "bougies_analysees": len(variables),
        "psi_median": round(float(np.median(valides)), 4) if valides else None,
        "psi_maximum": round(float(max(valides)), 4) if valides else None,
        "variables_en_derive_forte": len(fortes),
        "variables_en_derive_moderee": len(moderees),
        "verdict": verdict,
        "detail": lignes,
    }
