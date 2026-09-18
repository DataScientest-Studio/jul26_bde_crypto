"""Tests des variables explicatives.

Deux proprietes que le modele suppose, et qu'aucun test ne verifiait :

  1. INDEPENDANCE DE LA FENETRE. L'entrainement calcule les variables sur des
     centaines de milliers de bougies ; l'API sur les quelques centaines
     qu'elle lit en base. Si une variable change de valeur selon la quantite
     d'historique fournie, le modele recoit en production autre chose que ce
     qu'il a appris - sans qu'aucune erreur ne se declenche.

     C'est arrive : `obv_normalise` valait 0,0089 avec l'historique complet et
     -0,0318 sur une fenetre de 500 bougies, parce que l'OBV est un cumul
     depuis le debut de la serie.

  2. AUCUN REGARD VERS LE FUTUR. Ajouter des bougies APRES un instant donne ne
     doit rien changer aux variables calculees a cet instant.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.features import FAMILLES, colonnes_explicatives, construire_groupes


@pytest.fixture
def bougies() -> pd.DataFrame:
    """Serie realiste : marche aleatoire, volumes variables."""
    alea = np.random.default_rng(7)
    n = 800
    temps = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    prix = 50_000 + np.cumsum(alea.normal(0, 40, n))
    return pd.DataFrame({
        "symbol": "BTCUSDT", "interval": "15m", "open_time": temps,
        "open": prix, "close": prix + alea.normal(0, 20, n),
        "high": prix + abs(alea.normal(30, 15, n)), "low": prix - abs(alea.normal(30, 15, n)),
        "volume": abs(alea.normal(100, 30, n)), "quote_volume": abs(alea.normal(5e6, 1e6, n)),
        "nb_trades": alea.integers(500, 1500, n), "taker_buy_base": abs(alea.normal(55, 10, n)),
    })


def test_variables_independantes_de_la_fenetre(bougies):
    """Calculer sur 800 bougies ou sur les 400 dernieres doit donner pareil.

    On compare les 100 dernieres lignes : les premieres d'une fenetre courte
    sont forcement incompletes (une moyenne mobile 100 a besoin de 100 bougies).
    """
    completes = construire_groupes(bougies, FAMILLES).tail(100).reset_index(drop=True)
    tronquees = construire_groupes(bougies.tail(400), FAMILLES).tail(100).reset_index(drop=True)

    # Tolerance RELATIVE : le RSI et l'ADX reposent sur un lissage
    # exponentiel, qui garde une trace infime de tout le passe. Les valeurs
    # convergent sans jamais coincider au dernier chiffre - on mesure des
    # ecarts de l'ordre de 10^-8 sur une echelle de 0 a 100, sans consequence.
    # Un vrai bug, lui, se voit tout de suite : obv_normalise ecartait de 0,27.
    fautives = {}
    for colonne in colonnes_explicatives(completes):
        a, b = completes[colonne].to_numpy(float), tronquees[colonne].to_numpy(float)
        valides = np.isfinite(a) & np.isfinite(b)
        if not valides.any():
            continue
        if not np.allclose(a[valides], b[valides], rtol=1e-5, atol=1e-9):
            fautives[colonne] = float(np.max(np.abs(a[valides] - b[valides])))

    assert not fautives, f"variables dependantes de la fenetre : {fautives}"


def test_aucun_regard_vers_le_futur(bougies):
    """Ajouter des bougies apres coup ne doit rien changer a ce qui precede."""
    passe = construire_groupes(bougies.head(600), FAMILLES).tail(50)
    complet = construire_groupes(bougies, FAMILLES).iloc[550:600]

    for colonne in colonnes_explicatives(passe):
        a, b = passe[colonne].to_numpy(float), complet[colonne].to_numpy(float)
        valides = np.isfinite(a) & np.isfinite(b)
        assert np.allclose(a[valides], b[valides], atol=1e-9), f"{colonne} regarde le futur"


def test_obv_reste_borne(bougies):
    """La version corrigee est un rapport de volumes : elle vit entre -1 et 1."""
    obv = construire_groupes(bougies, FAMILLES)["obv_normalise"].dropna()
    assert not obv.empty
    assert obv.between(-1, 1).all()


def test_variables_sans_echelle(bougies):
    """Doubler le prix ne doit pas doubler les variables.

    C'est la regle qui permet d'entrainer UN modele sur cinq paires : une
    bougie de BTC a 50 000 et une de XRP a 3 doivent se ressembler.
    """
    doubles = bougies.copy()
    for colonne in ("open", "high", "low", "close"):
        doubles[colonne] *= 2
    doubles["quote_volume"] *= 2

    normales = construire_groupes(bougies, FAMILLES).tail(100)
    grandes = construire_groupes(doubles, FAMILLES).tail(100)

    for colonne in colonnes_explicatives(normales):
        a, b = normales[colonne].to_numpy(float), grandes[colonne].to_numpy(float)
        valides = np.isfinite(a) & np.isfinite(b)
        assert np.allclose(a[valides], b[valides], rtol=1e-6, atol=1e-9), \
            f"{colonne} depend du niveau de prix"
