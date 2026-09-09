"""Etiquetage par la methode des trois barrieres (Lopez de Prado, 2018).

Pour chaque bougie on pose trois barrieres et on regarde laquelle est
touchee EN PREMIER :

    barriere haute  (take profit) -> +1  acheter
    barriere basse  (stop loss)   -> -1  vendre
    barriere verticale (echeance) ->  0  ne rien faire

Deux raisons de preferer cette methode a un simple "le prix monte-t-il
dans N bougies ?" :

  1. Le stop loss et le take profit ne sont pas ajoutes apres coup : ils
     definissent ce que le modele apprend. Un systeme de trading complet
     en decoule naturellement.
  2. On utilise le HAUT et le BAS de chaque bougie, pas la cloture. Un
     ordre stop se declenche en cours de bougie ; ignorer les meches
     donnerait des etiquettes que le marche reel n'aurait jamais produites.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view


def volatilite_glissante(close: pd.Series, fenetre: int = 24) -> pd.Series:
    """Ecart-type des rendements recents.

    Les barrieres sont exprimees en multiples de cette volatilite, et non
    en pourcentage fixe : un seuil de 2 % n'a pas le meme sens en periode
    calme et en pleine tempete. Des barrieres adaptatives donnent des
    classes stables dans le temps.
    """
    return close.pct_change().rolling(fenetre).std()


def triple_barriere(
    df: pd.DataFrame,
    largeur: float = 2.0,
    horizon: int = 24,
    fenetre_vol: int = 24,
) -> pd.DataFrame:
    """Etiquette un DataFrame OHLC d'une seule paire et d'un seul pas de temps.

    Retourne les colonnes : label (-1/0/1), barriere_haute, barriere_basse,
    bougies_avant_sortie, rendement_a_la_sortie.

    L'implementation est vectorisee : on construit une vue glissante des
    `horizon` bougies suivantes, sans copie memoire, puis on compare les
    barrieres d'un coup. Une boucle Python sur 1,6 million de lignes
    prendrait plusieurs minutes ; ici c'est instantane.
    """
    n = len(df)
    close = df["close"].to_numpy(dtype=np.float64)
    high = df["high"].to_numpy(dtype=np.float64)
    low = df["low"].to_numpy(dtype=np.float64)

    vol = volatilite_glissante(df["close"], fenetre_vol).to_numpy()
    haute = close * (1.0 + largeur * vol)
    basse = close * (1.0 - largeur * vol)

    label = np.full(n, np.nan)
    sortie = np.full(n, np.nan)
    rendement = np.full(n, np.nan)

    utilisables = n - horizon
    if utilisables <= 0:
        return _resultat(df, label, haute, basse, sortie, rendement)

    # Fenetres des `horizon` bougies SUIVANTES (on decale de 1).
    fen_high = sliding_window_view(high[1:], horizon)[:utilisables]
    fen_low = sliding_window_view(low[1:], horizon)[:utilisables]

    touche_haut = fen_high >= haute[:utilisables, None]
    touche_bas = fen_low <= basse[:utilisables, None]

    # argmax rend 0 quand aucune valeur n'est vraie : on distingue les deux
    # cas avec .any(), sinon "jamais touche" se confondrait avec "touche
    # des la premiere bougie".
    a_touche_haut = touche_haut.any(axis=1)
    a_touche_bas = touche_bas.any(axis=1)
    quand_haut = np.where(a_touche_haut, touche_haut.argmax(axis=1), horizon)
    quand_bas = np.where(a_touche_bas, touche_bas.argmax(axis=1), horizon)

    lab = np.zeros(utilisables)
    lab[quand_haut < quand_bas] = 1.0
    lab[quand_bas < quand_haut] = -1.0
    # Egalite stricte hors "aucune des deux" : la meme bougie touche les
    # deux barrieres. Impossible de savoir laquelle en premier sans les
    # donnees tick, on reste prudent et on n'etiquette pas.
    ambigu = (quand_haut == quand_bas) & a_touche_haut & a_touche_bas
    lab[ambigu] = 0.0

    # `horizon` sert de sentinelle "jamais touche" pour les comparaisons
    # ci-dessus. Pour le DELAI en revanche, la sortie se fait alors sur la
    # barriere verticale, soit la bougie `horizon` - pas horizon + 1.
    quand = np.minimum(np.minimum(quand_haut, quand_bas), horizon - 1)
    label[:utilisables] = lab
    sortie[:utilisables] = quand + 1
    indices_sortie = np.minimum(np.arange(utilisables) + quand + 1, n - 1)
    rendement[:utilisables] = close[indices_sortie] / close[:utilisables] - 1.0

    # Les premieres lignes n'ont pas assez d'historique pour la volatilite.
    label[: fenetre_vol] = np.nan
    return _resultat(df, label, haute, basse, sortie, rendement)


def _resultat(df, label, haute, basse, sortie, rendement) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "label": label,
            "barriere_haute": haute,
            "barriere_basse": basse,
            "bougies_avant_sortie": sortie,
            "rendement_a_la_sortie": rendement,
        },
        index=df.index,
    )


def etiqueter_groupes(
    df: pd.DataFrame, largeur: float = 2.0, horizon: int = 24, fenetre_vol: int = 24
) -> pd.DataFrame:
    """Applique l'etiquetage paire par paire et pas de temps par pas de temps.

    Indispensable : melanger deux paires ferait comparer le prix du BTC a
    celui de l'ETH d'une bougie a l'autre, et melanger deux pas de temps
    donnerait un horizon incoherent.
    """
    morceaux = []
    for (symbol, interval), groupe in df.groupby(["symbol", "interval"], sort=False):
        groupe = groupe.sort_values("open_time")
        resultat = triple_barriere(groupe, largeur, horizon, fenetre_vol)
        resultat["symbol"] = symbol
        resultat["interval"] = interval
        resultat["open_time"] = groupe["open_time"].to_numpy()
        morceaux.append(resultat)
    return pd.concat(morceaux, ignore_index=True)


def distribution(labels: pd.Series) -> dict:
    """Repartition des classes, en pourcentage.

    Un etiquetage dont une classe ecrase les autres produit un modele qui
    predit toujours la meme chose : c'est le premier chiffre a regarder.
    """
    valides = labels.dropna()
    if valides.empty:
        return {"achat": 0.0, "vente": 0.0, "neutre": 0.0, "n": 0}
    parts = valides.value_counts(normalize=True) * 100
    return {
        "achat": round(parts.get(1.0, 0.0), 1),
        "vente": round(parts.get(-1.0, 0.0), 1),
        "neutre": round(parts.get(0.0, 0.0), 1),
        "n": len(valides),
    }
