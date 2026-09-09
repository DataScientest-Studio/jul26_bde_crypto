"""Tests de l'etiquetage par trois barrieres.

C'est le module le plus critique du projet : une erreur ici ne fait pas
planter le programme, elle produit des etiquettes fausses que le modele
apprend consciencieusement. Les scores paraitraient excellents et le
resultat serait sans valeur.

On teste donc surtout ce qui ne se voit pas : l'absence de fuite du futur.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.labeling import (
    distribution,
    etiqueter_groupes,
    triple_barriere,
    volatilite_glissante,
)


def serie(closes, amplitude=0.0) -> pd.DataFrame:
    """Construit un DataFrame OHLC a partir d'une suite de cloture.

    `amplitude` ajoute des meches proportionnelles : sans elles, le haut et
    le bas valent la cloture et on ne testerait pas le declenchement en
    cours de bougie.
    """
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "open": closes,
        "high": closes * (1 + amplitude),
        "low": closes * (1 - amplitude),
        "close": closes,
        "volume": np.ones(len(closes)),
    })


# --- Comportement de base --------------------------------------------------

def test_hausse_reguliere_donne_des_achats():
    """Un prix qui monte sans discontinuer doit toucher la barriere haute."""
    prix = 100 * (1.01 ** np.arange(200))
    resultat = triple_barriere(serie(prix), largeur=2.0, horizon=10, fenetre_vol=24)
    labels = resultat["label"].dropna()
    assert (labels == 1).mean() > 0.9, "une hausse continue devrait etiqueter +1"


def test_baisse_reguliere_donne_des_ventes():
    prix = 100 * (0.99 ** np.arange(200))
    resultat = triple_barriere(serie(prix), largeur=2.0, horizon=10, fenetre_vol=24)
    labels = resultat["label"].dropna()
    assert (labels == -1).mean() > 0.9, "une baisse continue devrait etiqueter -1"


def test_barrieres_encadrent_le_prix():
    prix = 100 + np.random.default_rng(0).normal(0, 1, 300).cumsum()
    df = serie(prix)
    resultat = triple_barriere(df, largeur=2.0, horizon=10)
    valides = resultat.dropna(subset=["barriere_haute"])
    assert (valides["barriere_haute"] > df.loc[valides.index, "close"]).all()
    assert (valides["barriere_basse"] < df.loc[valides.index, "close"]).all()


def test_barrieres_plus_larges_donnent_plus_de_neutres():
    """Plus les barrieres sont loin, plus l'echeance arrive avant elles."""
    prix = 100 + np.random.default_rng(1).normal(0, 1, 500).cumsum()
    df = serie(prix)
    etroites = distribution(triple_barriere(df, largeur=1.0, horizon=10)["label"])
    larges = distribution(triple_barriere(df, largeur=5.0, horizon=10)["label"])
    assert larges["neutre"] > etroites["neutre"]


# --- Absence de fuite du futur : le test central ---------------------------

def test_aucune_fuite_du_futur():
    """L'etiquette d'une bougie ne doit dependre QUE des `horizon` bougies
    qui la suivent.

    Methode : on etiquette une serie, puis on la tronque et on re-etiquette.
    Toute etiquette calculable avant la troncature doit etre identique. Si
    elle change, c'est que de l'information venue d'apres a fuite.
    """
    prix = 100 + np.random.default_rng(2).normal(0, 1, 400).cumsum()
    horizon = 10

    complet = triple_barriere(serie(prix), largeur=2.0, horizon=horizon)
    tronque = triple_barriere(serie(prix[:250]), largeur=2.0, horizon=horizon)

    # Les lignes dont la fenetre tient entierement dans la partie tronquee.
    zone = slice(0, 250 - horizon - 1)
    a = complet["label"].iloc[zone]
    b = tronque["label"].iloc[zone]
    pd.testing.assert_series_equal(a, b, check_names=False)


def test_la_volatilite_ne_regarde_pas_devant():
    """La volatilite qui dimensionne les barrieres doit etre calculee sur le
    passe. Un pic de volatilite futur ne doit pas elargir une barriere
    anterieure."""
    base = np.full(100, 100.0)
    calme = base.copy()
    agite = base.copy()
    agite[60:] *= np.linspace(1, 3, 40)  # tempete a partir de la ligne 60

    vol_calme = volatilite_glissante(pd.Series(calme), 24)
    vol_agite = volatilite_glissante(pd.Series(agite), 24)
    # Avant la ligne 60, les deux series sont identiques : les volatilites
    # doivent l'etre aussi.
    pd.testing.assert_series_equal(vol_calme.iloc[:60], vol_agite.iloc[:60])


def test_la_periode_de_chauffe_n_est_pas_etiquetee():
    """Sans assez d'historique, la volatilite n'a pas de sens : ces lignes
    doivent rester sans etiquette plutot que d'en recevoir une fausse."""
    prix = 100 + np.random.default_rng(3).normal(0, 1, 200).cumsum()
    resultat = triple_barriere(serie(prix), largeur=2.0, horizon=10, fenetre_vol=30)
    assert resultat["label"].iloc[:30].isna().all()


def test_les_dernieres_bougies_n_ont_pas_d_etiquette():
    """Les `horizon` dernieres bougies n'ont pas de futur observable : leur
    etiqueter quoi que ce soit serait une invention."""
    prix = 100 + np.random.default_rng(4).normal(0, 1, 200).cumsum()
    horizon = 15
    resultat = triple_barriere(serie(prix), largeur=2.0, horizon=horizon)
    assert resultat["label"].iloc[-horizon:].isna().all()


# --- Declenchement en cours de bougie --------------------------------------

def test_les_meches_declenchent_les_barrieres_plus_tot():
    """Un stop se declenche au plus bas de la bougie, pas a sa cloture.

    On compare la MEME serie avec et sans meches : les meches doivent faire
    sortir plus tot, puisqu'elles atteignent la barriere avant que la
    cloture n'y arrive. Ignorer ce mecanisme produirait des etiquettes que
    le marche reel n'aurait jamais donnees.
    """
    # Vraie marche aleatoire : une serie plate donnerait une volatilite
    # nulle, donc des barrieres collees au prix, et le test ne mesurerait
    # plus rien.
    rng = np.random.default_rng(5)
    prix = 100 * (1 + rng.normal(0, 0.01, 400)).cumprod()

    sans_meche = triple_barriere(serie(prix, amplitude=0.0), largeur=2.0, horizon=20)
    avec_meche = triple_barriere(serie(prix, amplitude=0.015), largeur=2.0, horizon=20)

    sortie_sans = sans_meche["bougies_avant_sortie"].dropna().mean()
    sortie_avec = avec_meche["bougies_avant_sortie"].dropna().mean()
    assert sortie_avec < sortie_sans, (
        f"avec meches on devrait sortir plus tot : {sortie_avec:.1f} vs {sortie_sans:.1f}"
    )


# --- Cohesion des groupes --------------------------------------------------

def test_les_paires_ne_se_melangent_pas():
    """Etiqueter deux paires ensemble comparerait le prix du BTC a celui de
    l'ETH d'une bougie a l'autre."""
    rng = np.random.default_rng(6)
    lignes = []
    for symbole, base in (("BTCUSDT", 60000.0), ("ETHUSDT", 3000.0)):
        prix = base * (1 + rng.normal(0, 0.01, 200)).cumprod()
        d = serie(prix)
        d["symbol"] = symbole
        d["interval"] = "1h"
        d["open_time"] = pd.date_range("2026-01-01", periods=200, freq="h", tz="UTC")
        lignes.append(d)

    resultat = etiqueter_groupes(pd.concat(lignes, ignore_index=True),
                                 largeur=2.0, horizon=10)
    # Chaque paire doit avoir sa propre periode de chauffe non etiquetee.
    for symbole in ("BTCUSDT", "ETHUSDT"):
        sous = resultat[resultat["symbol"] == symbole]
        assert sous["label"].isna().sum() >= 24 + 10


def test_les_pas_de_temps_ne_se_melangent_pas():
    rng = np.random.default_rng(7)
    lignes = []
    for pas in ("1h", "4h"):
        prix = 60000 * (1 + rng.normal(0, 0.01, 200)).cumprod()
        d = serie(prix)
        d["symbol"] = "BTCUSDT"
        d["interval"] = pas
        d["open_time"] = pd.date_range("2026-01-01", periods=200, freq="h", tz="UTC")
        lignes.append(d)

    resultat = etiqueter_groupes(pd.concat(lignes, ignore_index=True),
                                 largeur=2.0, horizon=10)
    assert set(resultat["interval"].unique()) == {"1h", "4h"}
    assert len(resultat) == 400


# --- Cas limites -----------------------------------------------------------

def test_prix_parfaitement_plat():
    """Volatilite nulle : aucune barriere n'a de sens, rien ne doit etre
    etiquete plutot que de diviser par zero."""
    resultat = triple_barriere(serie(np.full(100, 100.0)), largeur=2.0, horizon=10)
    labels = resultat["label"].dropna()
    assert (labels == 0).all() or labels.empty


def test_serie_plus_courte_que_l_horizon():
    resultat = triple_barriere(serie(np.full(5, 100.0)), largeur=2.0, horizon=10)
    assert resultat["label"].isna().all()
    assert len(resultat) == 5


@pytest.mark.parametrize("horizon", [1, 5, 24, 48])
def test_le_delai_de_sortie_respecte_l_horizon(horizon):
    """Une sortie annoncee au-dela de l'horizon signalerait une erreur
    d'indice dans la fenetre glissante."""
    prix = 100 + np.random.default_rng(8).normal(0, 1, 300).cumsum()
    resultat = triple_barriere(serie(prix), largeur=2.0, horizon=horizon)
    sorties = resultat["bougies_avant_sortie"].dropna()
    assert (sorties >= 1).all()
    assert (sorties <= horizon).all()


def test_distribution_somme_a_cent():
    prix = 100 + np.random.default_rng(9).normal(0, 1, 300).cumsum()
    d = distribution(triple_barriere(serie(prix), largeur=2.0, horizon=10)["label"])
    assert abs(d["achat"] + d["vente"] + d["neutre"] - 100) < 0.5
    assert d["n"] > 0
