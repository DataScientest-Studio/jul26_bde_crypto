"""Simulation d'une strategie : combien d'argent aurait-elle rapporte ?

Difference avec l'evaluation d'un modele :

  evaluer un modele  -> "la prediction est-elle juste ?"  (des pourcentages)
  backtester         -> "combien ca rapporte ?"           (des euros)

L'ecart entre les deux est enorme. Un modele a 51 % de bon sens semble
correct, mais avec 0,2 % de frais par aller-retour il peut perdre de
l'argent a chaque operation. Seul le backtest le montre.

Deux precautions qui separent un backtest honnete d'un backtest flatteur :

  1. Positions NON CHEVAUCHANTES. Entrer a chaque bougie reviendrait a
     detenir des dizaines de positions simultanees, avec un capital qu'on
     n'a pas. On attend la sortie avant de reprendre un signal.

  2. Frais preleves aux DEUX bouts. 0,1 % a l'entree, 0,1 % a la sortie,
     soit 0,2 % qu'il faut battre avant de gagner un centime.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Frais Binance au tarif standard, par ordre. Verifie en septembre 2026.
FRAIS_PAR_ORDRE = 0.001


def simuler(
    signaux: pd.Series,
    rendements_sortie: pd.Series,
    delais_sortie: pd.Series,
    capital_initial: float = 10_000.0,
    frais: float = FRAIS_PAR_ORDRE,
    fraction_engagee: float = 1.0,
    retourner_courbe: bool = False,
) -> dict:
    """Rejoue une suite de signaux et retourne le resultat financier.

    `rendements_sortie` et `delais_sortie` viennent de l'etiquetage : ils
    disent ce qu'aurait rapporte une position ouverte a cette bougie et
    fermee a la barriere touchee. On ne simule donc pas les prix, on
    reutilise ce que le marche a REELLEMENT fait.

    Un signal a +1 ouvre une position acheteuse, -1 une vendeuse, 0 ne fait
    rien.
    """
    signaux = np.asarray(signaux)
    rendements = np.asarray(rendements_sortie, dtype=float)
    delais = np.asarray(delais_sortie, dtype=float)

    capital = capital_initial
    courbe = [capital_initial]
    operations = []

    i, n = 0, len(signaux)
    while i < n:
        signal = signaux[i]
        if signal == 0 or not np.isfinite(rendements[i]) or not np.isfinite(delais[i]):
            i += 1
            continue

        # Le rendement du marche, oriente selon le sens de la position :
        # une position vendeuse gagne quand le prix baisse.
        rendement_brut = rendements[i] * signal
        # Les frais s'appliquent a l'entree ET a la sortie.
        rendement_net = (1 + rendement_brut) * (1 - frais) ** 2 - 1

        gain = capital * fraction_engagee * rendement_net
        capital += gain
        courbe.append(capital)
        operations.append({
            "indice": i,
            "sens": int(signal),
            "rendement_brut": rendement_brut,
            "rendement_net": rendement_net,
            "capital": capital,
        })

        # On saute jusqu'a la sortie : pas de positions superposees.
        i += max(int(delais[i]), 1)

    resultat = _resultat(capital_initial, capital, courbe, operations, frais)
    if retourner_courbe:
        # Pour tracer l'evolution du capital. Absent par defaut : les JSON de
        # resultats n'ont pas a porter des milliers de valeurs.
        resultat["courbe"] = [round(float(c), 2) for c in courbe]
    return resultat


def _resultat(capital_initial, capital, courbe, operations, frais) -> dict:
    if not operations:
        return {"operations": 0, "capital_final": capital_initial,
                "performance_pct": 0.0, "note": "aucun signal exploitable"}

    ops = pd.DataFrame(operations)
    courbe = np.array(courbe)

    # Perte maximale depuis un sommet : ce qu'un investisseur aurait
    # reellement vecu, et souvent ce qui fait abandonner une strategie.
    sommets = np.maximum.accumulate(courbe)
    pertes = (courbe - sommets) / sommets

    gagnantes = ops["rendement_net"] > 0
    ecart_type = ops["rendement_net"].std()

    return {
        "operations": len(ops),
        "capital_initial": round(capital_initial, 2),
        "capital_final": round(capital, 2),
        "performance_pct": round((capital / capital_initial - 1) * 100, 2),
        "taux_reussite_pct": round(gagnantes.mean() * 100, 2),
        "gain_moyen_brut_pct": round(ops["rendement_brut"].mean() * 100, 4),
        "gain_moyen_net_pct": round(ops["rendement_net"].mean() * 100, 4),
        # Ce que les frais ont coute au total, en points de rendement.
        "cout_total_frais_pct": round(len(ops) * 2 * frais * 100, 2),
        "perte_max_pct": round(pertes.min() * 100, 2),
        # Rendement par unite de risque. Sans annualisation : les profils
        # n'ont pas la meme frequence, une annualisation les rendrait
        # incomparables.
        "ratio_rendement_risque": (round(ops["rendement_net"].mean() / ecart_type, 4)
                                   if ecart_type > 0 else None),
    }


def simuler_parfait(rendements_sortie, delais_sortie, labels, **kwargs) -> dict:
    """Meme simulation avec un modele PARFAIT, qui connaitrait l'avenir.

    C'est le plafond absolu de la strategie. S'il est deja faible, le
    probleme ne vient pas du modele mais de l'etiquetage ou des frais - et
    aucun reglage de modele n'y changera rien.
    """
    return simuler(np.asarray(labels), rendements_sortie, delais_sortie, **kwargs)
