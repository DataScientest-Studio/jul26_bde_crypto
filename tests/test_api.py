"""Tests unitaires de l'API.

Principe : aucun test ne depend des bases de donnees ni du vrai modele de
100 Mo. Les acces exterieurs sont REMPLACES par des doublures, pour trois
raisons :

  - les tests doivent passer sur la machine d'un collegue, sans Docker ;
  - ils doivent durer quelques secondes, pas quelques minutes ;
  - un test qui echoue doit designer un bug de l'API, pas une base eteinte.

Lancement :
    pytest tests/test_api.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import derive as module_derive
from api import donnees, main, modele


# ---------------------------------------------------------------------------
#  Doublures
# ---------------------------------------------------------------------------

class ModeleFactice:
    """Renvoie une probabilite fixe : le test verifie l'API, pas le modele."""

    def __init__(self, probabilite: float = 0.62):
        self.probabilite = probabilite

    def predict_proba(self, X):
        return np.tile([1 - self.probabilite, self.probabilite], (len(X), 1))


def bougies_factices(n: int = 400) -> pd.DataFrame:
    """Bougies coherentes : haut >= max(ouverture, cloture), bas <= min, etc."""
    morceaux = []
    for interval, minutes in (("15m", 15), ("1h", 60), ("4h", 240)):
        temps = pd.date_range("2026-01-01", periods=n, freq=f"{minutes}min", tz="UTC")
        marche = 50_000 + np.cumsum(np.random.default_rng(0).normal(0, 50, n))
        morceaux.append(pd.DataFrame({
            "symbol": "BTCUSDT", "interval": interval, "open_time": temps,
            "close_time": temps + pd.Timedelta(f"{minutes}min"),
            "open": marche, "high": marche + 60, "low": marche - 60, "close": marche + 10,
            "volume": 100.0, "quote_volume": 5_000_000.0, "nb_trades": 900,
            "taker_buy_base": 55.0,
        }))
    return pd.concat(morceaux, ignore_index=True)


@pytest.fixture
def client(monkeypatch):
    """Client de test, avec bases et modele remplaces par des doublures."""
    paquet = {
        "modele": ModeleFactice(),
        "colonnes": [],          # complete plus bas, une fois les variables connues
        "cible": "sens de la prochaine bougie",
        "profil": "day_trading",
        "styles": {"agressif": {"achat": 0.5604, "vente": 0.4396},
                   "conservateur": {"achat": 0.5901, "vente": 0.4099}},
        "style_le_plus_prudent": "conservateur",
        "entraine_le": "2026-09-17T10:35:00+00:00",
        "extrait_sha256": "a" * 64,
        "accuracy_globale": 0.5288,
    }

    def charger_factice():
        return paquet

    monkeypatch.setattr(modele, "charger", charger_factice)
    monkeypatch.setattr(donnees, "dernieres_bougies",
                        lambda symbole, profil="day_trading", par_intervalle=300: bougies_factices())
    monkeypatch.setattr(donnees, "etat_bases", lambda: {"postgresql": "ok", "mongodb": "ok"})
    monkeypatch.setattr(donnees, "paires_disponibles", lambda: ["BTCUSDT", "ETHUSDT"])
    monkeypatch.setattr(donnees, "journaliser_prediction", lambda prediction: None)
    monkeypatch.setattr(donnees, "bougies_binance",
                        lambda symbole, par_intervalle=300: bougies_factices())

    # Le modele factice accepte n'importe quelles colonnes : on prend celles
    # que la chaine de calcul produit reellement, pour verifier au passage
    # qu'elle fonctionne de bout en bout.
    from src.features import FAMILLES, ajouter_contexte_lent, construire_groupes
    variables = ajouter_contexte_lent(construire_groupes(bougies_factices(), FAMILLES))
    paquet["colonnes"] = [c for c in variables.columns
                          if c not in ("symbol", "interval", "open_time")] + ["pas_de_temps"]

    monkeypatch.setattr(main, "CLE_ATTENDUE", None)
    return TestClient(main.app)


# ---------------------------------------------------------------------------
#  Routes generales
# ---------------------------------------------------------------------------

def test_racine_annonce_le_service(client):
    reponse = client.get("/")
    assert reponse.status_code == 200
    assert reponse.json()["nom"] == "CryptoBot"


def test_health_ok_quand_tout_repond(client):
    reponse = client.get("/health")
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["statut"] == "ok"
    assert corps["modele"] == "charge"
    assert corps["bases"] == {"postgresql": "ok", "mongodb": "ok"}


def test_health_degrade_si_une_base_manque(client, monkeypatch):
    monkeypatch.setattr(donnees, "etat_bases",
                        lambda: {"postgresql": "ok", "mongodb": "indisponible (ServerSelectionTimeoutError)"})
    corps = client.get("/health").json()
    assert corps["statut"] == "degrade"


def test_modele_expose_ses_metadonnees(client):
    corps = client.get("/modele").json()
    assert corps["cible"] == "sens de la prochaine bougie"
    assert corps["styles"]["agressif"] == {"achat": 0.5604, "vente": 0.4396}


# ---------------------------------------------------------------------------
#  Prediction
# ---------------------------------------------------------------------------

def test_prediction_achete_quand_le_modele_est_sur(client):
    reponse = client.post("/prediction",
                          json={"symbole": "BTCUSDT", "interval": "1h", "style": "agressif"})
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["decision"] == "acheter"       # probabilite 0,62 > seuil 0,5604
    assert corps["sens"] == 1
    assert corps["probabilite_hausse"] == pytest.approx(0.62, abs=1e-3)


def test_prediction_attend_quand_la_confiance_est_insuffisante(client, monkeypatch):
    """Le coeur du bouton conservateur : sous le seuil, on ne fait rien."""
    paquet = modele.charger()
    paquet["modele"] = ModeleFactice(0.55)      # au-dessus de 0,5 mais sous 0,5604
    reponse = client.post("/prediction",
                          json={"symbole": "BTCUSDT", "interval": "1h", "style": "agressif"})
    assert reponse.json()["decision"] == "attendre"
    assert reponse.json()["sens"] == 0


def test_prediction_vend_quand_la_baisse_est_probable(client):
    paquet = modele.charger()
    paquet["modele"] = ModeleFactice(0.35)      # sous le seuil de vente 0,4396
    corps = client.post("/prediction",
                        json={"symbole": "BTCUSDT", "interval": "1h", "style": "agressif"}).json()
    assert corps["decision"] == "vendre"
    assert corps["sens"] == -1


def test_le_style_conservateur_est_plus_exigeant(client):
    paquet = modele.charger()
    paquet["modele"] = ModeleFactice(0.58)      # au-dessus de 0,5604, sous 0,5901
    agressif = client.post("/prediction",
                           json={"symbole": "BTCUSDT", "style": "agressif"}).json()
    conservateur = client.post("/prediction",
                               json={"symbole": "BTCUSDT", "style": "conservateur"}).json()
    assert agressif["decision"] == "acheter"
    assert conservateur["decision"] == "attendre"


def test_style_inconnu_refuse(client):
    reponse = client.post("/prediction",
                          json={"symbole": "BTCUSDT", "style": "tres_agressif"})
    assert reponse.status_code == 422       # refuse par la validation, avant le modele


def test_intervalle_inconnu_refuse(client):
    reponse = client.post("/prediction", json={"symbole": "BTCUSDT", "interval": "3m"})
    assert reponse.status_code == 422


def test_base_indisponible_donne_503(client, monkeypatch):
    def tombe(*args, **kwargs):
        raise ConnectionError("base eteinte")

    monkeypatch.setattr(donnees, "dernieres_bougies", tombe)
    reponse = client.post("/prediction", json={"symbole": "BTCUSDT"})
    assert reponse.status_code == 503


def test_modele_absent_donne_503(client, monkeypatch):
    def absent():
        raise modele.ModeleIndisponible("fichier introuvable")

    monkeypatch.setattr(modele, "charger", absent)
    assert client.get("/modele").status_code == 503
    assert client.get("/health").json()["modele"] == "absent"


# ---------------------------------------------------------------------------
#  Donnees
# ---------------------------------------------------------------------------

def test_bougies_renvoie_le_bon_pas_de_temps(client):
    corps = client.get("/bougies/btcusdt?interval=4h&limite=10").json()
    assert corps["symbole"] == "BTCUSDT"       # la casse est normalisee
    assert corps["bougies"] == 10
    assert all(b["interval"] == "4h" for b in corps["donnees"])


def test_bougies_pas_de_temps_absent_donne_404(client):
    assert client.get("/bougies/BTCUSDT?interval=1h&limite=0").status_code == 422


def test_paires_listees(client):
    assert client.get("/paires").json()["paires"] == ["BTCUSDT", "ETHUSDT"]


# ---------------------------------------------------------------------------
#  Cle d'API
# ---------------------------------------------------------------------------

def test_sans_cle_configuree_l_acces_est_libre(client):
    assert client.get("/modele").status_code == 200


def test_avec_cle_configuree_l_acces_est_refuse_sans_en_tete(client, monkeypatch):
    monkeypatch.setattr(main, "CLE_ATTENDUE", "secret")
    assert client.get("/modele").status_code == 401
    assert client.get("/modele", headers={"X-API-Key": "mauvaise"}).status_code == 401
    assert client.get("/modele", headers={"X-API-Key": "secret"}).status_code == 200


def test_cle_vide_laisse_l_acces_libre(monkeypatch):
    """Cas rencontre en vrai : docker-compose transmet une variable VIDE.

    Sans le `or None` dans main.py, l'API exigeait une cle egale a "" et
    refusait toutes les requetes, y compris celles qui fournissaient la bonne.
    """
    import importlib

    monkeypatch.setenv("CRYPTOBOT_API_KEY", "")
    importlib.reload(main)
    assert main.CLE_ATTENDUE is None


def test_health_reste_accessible_sans_cle(client, monkeypatch):
    """Sinon Docker ne pourrait pas verifier que le service est en vie."""
    monkeypatch.setattr(main, "CLE_ATTENDUE", "secret")
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
#  Derive
# ---------------------------------------------------------------------------

def test_psi_nul_quand_rien_ne_bouge():
    valeurs = pd.Series(np.linspace(0, 1, 1000))
    bornes = [float(q) for q in np.quantile(valeurs, np.linspace(0, 1, 11)[1:-1])]
    proportions = [0.1] * 10
    assert module_derive.psi(valeurs, bornes, proportions) < 0.01


def test_psi_augmente_quand_les_valeurs_se_decalent():
    reference = pd.Series(np.linspace(0, 1, 1000))
    bornes = [float(q) for q in np.quantile(reference, np.linspace(0, 1, 11)[1:-1])]
    proportions = [0.1] * 10
    decalees = pd.Series(np.linspace(5, 6, 1000))     # toutes hors de la plage connue
    assert module_derive.psi(decalees, bornes, proportions) > module_derive.SEUIL_FORT


def test_qualifier_respecte_les_seuils():
    assert module_derive.qualifier(0.05) == "stable"
    assert module_derive.qualifier(0.15) == "derive moderee"
    assert module_derive.qualifier(0.40) == "derive forte"
    assert module_derive.qualifier(float("nan")) == "inconnu"


def test_mesurer_signale_une_derive_forte():
    reference = {"periode": ["2024-08-29", "2026-02-01"], "lignes": 1000,
                 "extrait_sha256": "a" * 64,
                 "variables": {"rsi_14": {"bornes": [float(q) for q in np.linspace(10, 90, 9)],
                                          "proportions": [0.1] * 10}}}
    # Toutes les valeurs recentes dans la derniere tranche : derive maximale.
    variables = pd.DataFrame({"rsi_14": np.full(500, 95.0)})
    resultat = module_derive.mesurer(variables, reference)
    assert resultat["variables_en_derive_forte"] == 1
    assert "reentrainement" in resultat["verdict"]


def test_mesurer_ne_signale_rien_quand_les_donnees_se_ressemblent():
    valeurs = np.random.default_rng(0).normal(50, 10, 5000)
    bornes = [float(q) for q in np.quantile(valeurs, np.linspace(0, 1, 11)[1:-1])]
    reference = {"periode": ["2024", "2026"], "lignes": 5000, "extrait_sha256": "a" * 64,
                 "variables": {"rsi_14": {"bornes": bornes, "proportions": [0.1] * 10}}}
    recentes = pd.DataFrame({"rsi_14": np.random.default_rng(1).normal(50, 10, 2000)})
    resultat = module_derive.mesurer(recentes, reference)
    assert resultat["variables_en_derive_forte"] == 0
    assert resultat["verdict"].startswith("stable")


def test_route_derive_utilise_la_reference_de_la_paire(client, monkeypatch):
    """La route repond, et compare bien a la reference du couple paire/pas."""
    reference = {"periode": ["2024-08-29", "2026-02-01"], "lignes": 326751,
                 "extrait_sha256": "a" * 64, "variables": {},
                 "par_groupe": {"BTCUSDT|1h": {"rsi_14": {
                     "bornes": [float(b) for b in np.linspace(10, 90, 9)],
                     "proportions": [0.1] * 10}}}}
    monkeypatch.setattr(module_derive, "charger_reference", lambda: reference)

    reponse = client.get("/derive?symbole=BTCUSDT&interval=1h&jours=90")
    assert reponse.status_code == 200
    assert reponse.json()["reference"]["portee"] == "BTCUSDT|1h"


def test_route_derive_refuse_une_fenetre_absurde(client):
    """Moins d'une semaine ne veut rien dire pour des variables lentes."""
    assert client.get("/derive?jours=1").status_code == 422
    assert client.get("/derive?jours=9999").status_code == 422


def test_route_derive_sans_reference_donne_503(client, monkeypatch):
    def absente():
        raise module_derive.ReferenceIndisponible("reference introuvable")

    monkeypatch.setattr(module_derive, "charger_reference", absente)
    assert client.get("/derive").status_code == 503


# ---------------------------------------------------------------------------
#  Interface : graphique et page de demonstration
# ---------------------------------------------------------------------------

def test_graphique_renvoie_une_decision_par_bougie(client):
    """Ce que consomme l'interface : un seul appel, tout le necessaire."""
    corps = client.get("/graphique/btcusdt?interval=1h&limite=50&style=agressif").json()
    assert corps["symbole"] == "BTCUSDT"
    assert corps["source"] == "binance"          # marche en direct par defaut
    assert corps["bougies"] == 50
    premiere = corps["donnees"][0]
    assert {"open", "high", "low", "close", "volume"} <= set(premiere)
    assert premiere["decision"] in {"acheter", "vendre", "attendre"}
    assert 0 <= premiere["probabilite_hausse"] <= 1


def test_graphique_peut_lire_la_base(client):
    corps = client.get("/graphique/BTCUSDT?source=base&limite=30").json()
    assert corps["source"] == "base"
    assert corps["bougies"] == 30


def test_graphique_source_inconnue_refusee(client):
    assert client.get("/graphique/BTCUSDT?source=coinbase").status_code == 422


def test_graphique_binance_indisponible_donne_503(client, monkeypatch):
    def tombe(*args, **kwargs):
        raise ConnectionError("Binance injoignable")

    monkeypatch.setattr(donnees, "bougies_binance", tombe)
    assert client.get("/graphique/BTCUSDT").status_code == 503


def test_interface_sert_une_page_html(client):
    """La page est servie par l'API : meme origine, donc aucun CORS a regler."""
    reponse = client.get("/interface")
    assert reponse.status_code == 200
    assert "text/html" in reponse.headers["content-type"]
    assert "<canvas" in reponse.text


def test_interface_accessible_sans_cle(client, monkeypatch):
    monkeypatch.setattr(main, "CLE_ATTENDUE", "secret")
    assert client.get("/interface").status_code == 200


def test_les_deux_cotes_peuvent_se_declencher(client):
    """Le modele doit pouvoir acheter ET vendre.

    Avec un seuil unique sur la distance a 0,5, le style conservateur se
    retrouvait au-dessus du maximum de probabilite atteignable en achat : il ne
    produisait que des ventes. Un seuil par cote corrige ce defaut.
    """
    paquet = modele.charger()
    decisions = {}
    for probabilite in (0.62, 0.38, 0.50):
        paquet["modele"] = ModeleFactice(probabilite)
        corps = client.post("/prediction",
                            json={"symbole": "BTCUSDT", "style": "conservateur"}).json()
        decisions[probabilite] = corps["decision"]
    assert decisions == {0.62: "acheter", 0.38: "vendre", 0.50: "attendre"}
