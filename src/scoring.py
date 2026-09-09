"""Mesures de qualite adaptees a un bot de trading.

L'accuracy globale est trompeuse ici. Mesuree sur day_trading, elle donne
42,3 % contre 33,3 % pour le hasard - ce qui semble bon. Mais en isolant
les cas ou le modele passe un ordre ET ou le marche bouge vraiment, le
taux de bon sens tombe a 50,6 %, contre 50,0 % pour pile ou face.

Autrement dit, les 9 points d'avance viennent presque entierement de la
capacite a reconnaitre les phases calmes. Un bot ne gagne pas d'argent en
s'abstenant : il en gagne en ayant raison sur le SENS quand il agit.

C'est donc cette mesure qu'on optimise.
"""
from __future__ import annotations

import numpy as np
from sklearn.metrics import make_scorer


def bon_sens_directionnel(y_vrai, y_predit, minimum_ordres: int = 50) -> float:
    """Part des ordres passes dans le bon sens.

    On ne compte que les cas ou le modele agit (prediction non nulle) ET ou
    le marche a reellement bouge (verite non nulle). Les autres ne disent
    rien sur la capacite a predire une direction.

    Un modele qui n'agit presque jamais obtiendrait un score calcule sur
    trois ordres, donc ininterpretable : en dessous de `minimum_ordres` on
    renvoie 0,5, la valeur du hasard. Cela evite qu'une grille de recherche
    ne selectionne un modele muet.
    """
    y_vrai = np.asarray(y_vrai)
    y_predit = np.asarray(y_predit)

    concernes = (y_predit != 0) & (y_vrai != 0)
    if concernes.sum() < minimum_ordres:
        return 0.5
    return float((y_predit[concernes] == y_vrai[concernes]).mean())


def taux_activite(y_vrai, y_predit) -> float:
    """Part des bougies ou le modele passe un ordre.

    A surveiller a cote du bon sens : un modele qui agit 2 % du temps peut
    afficher un bon score sans jamais rien rapporter, faute d'occasions.
    """
    y_predit = np.asarray(y_predit)
    return float((y_predit != 0).mean())


# Scorer utilisable directement par GridSearchCV.
SCORER_DIRECTIONNEL = make_scorer(bon_sens_directionnel, greater_is_better=True)


def resume(y_vrai, y_predit) -> dict:
    """Les trois chiffres a regarder ensemble.

    Aucun ne suffit seul : un bon sens de 60 % sur 1 % des bougies ne vaut
    pas 52 % sur 40 % d'entre elles.
    """
    y_vrai = np.asarray(y_vrai)
    y_predit = np.asarray(y_predit)
    return {
        "bon_sens": round(bon_sens_directionnel(y_vrai, y_predit), 4),
        "taux_activite": round(taux_activite(y_vrai, y_predit), 4),
        "accuracy_globale": round(float((y_vrai == y_predit).mean()), 4),
        "ordres_evalues": int(((y_predit != 0) & (y_vrai != 0)).sum()),
    }
