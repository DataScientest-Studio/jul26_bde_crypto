"""Construction des variables explicatives a partir des bougies.

Regle absolue de ce module : TOUTES les variables doivent etre SANS ECHELLE.

Un modele qui recoit "prix = 63 021" apprend le niveau du BTC en 2026. Il
sera inutilisable sur l'ETH a 3 000, et inutilisable sur le BTC l'annee
suivante. On ne garde donc que des rapports, des pourcentages et des
positions relatives - jamais un prix ni un volume brut.

C'est aussi ce qui permet d'entrainer UN modele sur les cinq paires a la
fois : une fois sans echelle, une bougie de BTC et une bougie de XRP sont
comparables.

Les indicateurs viennent de la librairie `ta` (celle indiquee par Remy).
On n'utilise pas `add_all_ta_features` : elle produit 86 colonnes dont 231
paires correlees a plus de 0,95, et deux colonnes vides a plus de 45 %
(le Parabolic SAR n'existe que dans un sens de tendance).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from ta import momentum, trend, volatility, volume

# Familles de variables. Le decoupage sert a mesurer l'apport de chacune :
# on entraine avec une seule famille, puis avec toutes, et on compare.
FAMILLES = ("tendance", "momentum", "volatilite", "volume", "flux")


def _tendance(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Ou en est le prix par rapport a ses moyennes.

    On ne garde pas les moyennes elles-memes - ce sont des niveaux de prix -
    mais l'ECART RELATIF entre le prix et sa moyenne, qui est sans echelle.
    """
    close = df["close"]
    sortie = {}
    for periode in (10, 30, 100):
        moyenne = close.rolling(periode).mean()
        sortie[f"ecart_sma_{periode}"] = (close - moyenne) / moyenne
    ema_courte = trend.ema_indicator(close, window=12)
    ema_longue = trend.ema_indicator(close, window=26)
    sortie["ecart_emas"] = (ema_courte - ema_longue) / ema_longue
    # MACD rapporte au prix pour le rendre comparable entre paires.
    sortie["macd_relatif"] = trend.macd_diff(close) / close
    sortie["adx"] = trend.adx(df["high"], df["low"], close)  # deja en 0-100
    return sortie


def _momentum(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Vitesse et essoufflement du mouvement. Ces indicateurs sont deja
    bornes, donc sans echelle par construction."""
    close = df["close"]
    sortie = {
        "rsi_14": momentum.rsi(close, window=14),
        "stoch_k": momentum.stoch(df["high"], df["low"], close),
        "roc_10": momentum.roc(close, window=10),
    }
    # On n'ajoute PAS Williams %R : il correle a 1,000 avec le Stochastique,
    # c'est mathematiquement la meme information.
    for horizon in (1, 3, 12):
        sortie[f"rendement_{horizon}"] = close.pct_change(horizon)
    return sortie


def _volatilite(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Amplitude des mouvements, toujours rapportee au prix."""
    close, haut, bas = df["close"], df["high"], df["low"]
    atr = volatility.average_true_range(haut, bas, close, window=14)
    bandes = volatility.BollingerBands(close, window=20, window_dev=2)
    return {
        "atr_relatif": atr / close,
        # Position dans les bandes : 0 = bande basse, 1 = bande haute.
        "position_bollinger": bandes.bollinger_pband(),
        "largeur_bollinger": bandes.bollinger_wband(),
        "vol_24": close.pct_change().rolling(24).std(),
        "amplitude_bougie": (haut - bas) / close,
        # Ou la cloture se situe dans la bougie : proche du haut = pression
        # acheteuse jusqu'au bout.
        "position_cloture": (close - bas) / (haut - bas).replace(0, np.nan),
    }


def _volume(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Le volume brut depend de la paire : on ne garde que des rapports."""
    vol = df["volume"]
    moyenne = vol.rolling(24).mean()
    return {
        "volume_relatif": vol / moyenne,
        "obv_normalise": volume.on_balance_volume(df["close"], vol).pct_change(12),
        "mfi": volume.money_flow_index(df["high"], df["low"], df["close"], vol),
        "trades_relatifs": df["nb_trades"] / df["nb_trades"].rolling(24).mean(),
    }


def _flux(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Pression acheteuse, calculee depuis les champs `taker_buy_*`.

    Ces colonnes viennent de Binance et ne figurent dans AUCUNE librairie
    d'indicateurs : elles disent quelle part du volume a ete initiee par un
    acheteur pressé. C'est une information de carnet d'ordres agregee, que
    les autres equipes du cursus n'auront probablement pas.
    """
    part_acheteuse = df["taker_buy_base"] / df["volume"].replace(0, np.nan)
    return {
        "part_acheteuse": part_acheteuse,
        "part_acheteuse_moy_12": part_acheteuse.rolling(12).mean(),
        # Ecart a la NORME RECENTE, et non a 0,5 : "part_acheteuse - 0,5"
        # serait une simple translation, donc la meme variable (correlation
        # de 1,000 mesuree). Ici on capte un changement de regime : les
        # acheteurs sont-ils plus presents que d'habitude ?
        "flux_vs_habitude": part_acheteuse - part_acheteuse.rolling(24).mean(),
        "taille_trade_moyen": (df["quote_volume"] / df["nb_trades"].replace(0, np.nan)
                               / df["close"]),
    }


_CONSTRUCTEURS = {
    "tendance": _tendance,
    "momentum": _momentum,
    "volatilite": _volatilite,
    "volume": _volume,
    "flux": _flux,
}


def construire(df: pd.DataFrame, familles: tuple[str, ...] = FAMILLES) -> pd.DataFrame:
    """Calcule les variables d'un groupe (une paire, un pas de temps).

    Toutes les fonctions appelees sont retrospectives : elles n'utilisent
    que la bougie courante et les precedentes. Un indicateur qui regarderait
    devant creerait une fuite invisible aux tests classiques.
    """
    df = df.sort_values("open_time")
    colonnes: dict[str, pd.Series] = {}
    for famille in familles:
        colonnes.update(_CONSTRUCTEURS[famille](df))
    resultat = pd.DataFrame(colonnes, index=df.index)
    # Les indicateurs peuvent produire des infinis (divisions par un volume
    # nul). On les neutralise ici plutot que de les laisser faire exploser
    # la normalisation.
    return resultat.replace([np.inf, -np.inf], np.nan)


def construire_groupes(
    df: pd.DataFrame, familles: tuple[str, ...] = FAMILLES
) -> pd.DataFrame:
    """Applique le calcul paire par paire et pas de temps par pas de temps.

    Calculer une moyenne mobile a cheval sur deux paires melangerait le prix
    du BTC et celui de l'ETH. Le decoupage est donc obligatoire, pas une
    precaution.
    """
    morceaux = []
    for (symbol, interval), groupe in df.groupby(["symbol", "interval"], sort=False):
        variables = construire(groupe, familles)
        variables["symbol"] = symbol
        variables["interval"] = interval
        variables["open_time"] = groupe.sort_values("open_time")["open_time"].to_numpy()
        morceaux.append(variables)
    return pd.concat(morceaux, ignore_index=True)


def colonnes_explicatives(df: pd.DataFrame) -> list[str]:
    """Colonnes a donner au modele : tout sauf les identifiants."""
    exclues = {"symbol", "interval", "open_time", "label"}
    return [c for c in df.columns if c not in exclues]
