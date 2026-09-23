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
from api import donnees, main, modele, securite
from api import ordre as module_ordre
from api import positions as module_positions

# Cle de test : aleatoire en apparence, et assez longue pour etre acceptee.
CLE_DE_TEST = "0123456789abcdef0123456789abcdef"


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
                        lambda symbole, par_intervalle=300, inclure_en_cours=False: bougies_factices())

    # Le modele factice accepte n'importe quelles colonnes : on prend celles
    # que la chaine de calcul produit reellement, pour verifier au passage
    # qu'elle fonctionne de bout en bout.
    from src.features import FAMILLES, ajouter_contexte_lent, construire_groupes
    variables = ajouter_contexte_lent(construire_groupes(bougies_factices(), FAMILLES))
    paquet["colonnes"] = [c for c in variables.columns
                          if c not in ("symbol", "interval", "open_time")] + ["pas_de_temps"]

    # L'API est fermee par defaut : le client de test s'identifie comme un
    # vrai client, avec une cle, sur chaque requete.
    monkeypatch.setattr(securite, "CLES", {CLE_DE_TEST: "tests"})
    monkeypatch.setattr(securite, "ACCES_LIBRE", False)
    return TestClient(main.app, headers={"X-API-Key": CLE_DE_TEST})


@pytest.fixture
def anonyme(client):
    """Meme API, mais un client qui ne presente aucune cle."""
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

def test_sans_cle_l_acces_est_refuse(anonyme):
    reponse = anonyme.get("/modele")
    assert reponse.status_code == 401
    assert reponse.headers["www-authenticate"] == "ApiKey"


def test_cle_invalide_refusee_avec_le_meme_message(anonyme):
    """Absente ou fausse : meme reponse, rien a apprendre pour un attaquant."""
    absente = anonyme.get("/modele")
    fausse = anonyme.get("/modele", headers={"X-API-Key": "f" * 32})
    assert fausse.status_code == 401
    assert fausse.json() == absente.json()


def test_bonne_cle_acceptee(client):
    assert client.get("/modele").status_code == 200


def test_api_fermee_si_aucune_cle_configuree(anonyme, monkeypatch):
    """Un oubli de configuration ne doit jamais OUVRIR l'API."""
    monkeypatch.setattr(securite, "CLES", {})
    reponse = anonyme.get("/modele", headers={"X-API-Key": CLE_DE_TEST})
    assert reponse.status_code == 503
    assert "aucune cle" in reponse.json()["detail"]


def test_acces_libre_seulement_sur_demande_explicite(anonyme, monkeypatch):
    monkeypatch.setattr(securite, "CLES", {})
    monkeypatch.setattr(securite, "ACCES_LIBRE", True)
    assert anonyme.get("/modele").status_code == 200


def test_chaque_client_a_sa_cle():
    cles = securite.lire_cles("interface:" + "a" * 32 + ", airflow:" + "b" * 32)
    assert cles == {"a" * 32: "interface", "b" * 32: "airflow"}


def test_cles_trop_courtes_ou_mal_formees_ecartees():
    """Mieux vaut une cle refusee au demarrage qu'une cle faible acceptee."""
    cles = securite.lire_cles("interface:courte,sans-separateur," + ":" + "c" * 32)
    assert cles == {}


def test_variable_vide_ne_donne_aucune_cle():
    """Cas reel : docker-compose transmet une variable VIDE si elle manque."""
    assert securite.lire_cles("") == {}
    assert securite.lire_cles(None) == {}


def test_identification_du_client(monkeypatch):
    monkeypatch.setattr(securite, "CLES", {"a" * 32: "interface", "b" * 32: "airflow"})
    assert securite.identifier("b" * 32) == "airflow"
    assert securite.identifier("a" * 31 + "x") is None
    assert securite.identifier(None) is None


def test_health_reste_accessible_sans_cle(anonyme):
    """Sinon Docker ne pourrait pas verifier que le service est en vie."""
    assert anonyme.get("/health").status_code == 200


def test_documentation_accessible_sans_cle(anonyme):
    assert anonyme.get("/openapi.json").status_code == 200


def test_toutes_les_routes_de_donnees_sont_protegees(anonyme):
    """Garde-fou : une route ajoutee sans la dependance serait detectee ici."""
    publiques = {"/", "/health", "/metrics", "/openapi.json", "/docs",
                 "/docs/oauth2-redirect", "/redoc"}
    for route in main.app.routes:
        if route.path in publiques:
            continue
        chemin = route.path.replace("{symbole}", "BTCUSDT")
        methode = sorted(route.methods)[0]
        reponse = anonyme.request(methode, chemin)
        assert reponse.status_code == 401, f"{methode} {route.path} accessible sans cle"


# ---------------------------------------------------------------------------
#  Validation des entrees
# ---------------------------------------------------------------------------

def test_paire_inconnue_refusee_avant_binance(client, monkeypatch):
    """Une paire hors liste ne doit jamais partir dans une URL Binance."""
    appels = []
    monkeypatch.setattr(donnees, "bougies_binance",
                        lambda *a, **k: appels.append(a) or bougies_factices())
    reponse = client.get("/graphique/DOGEUSDT")
    assert reponse.status_code == 404
    assert "paire inconnue" in reponse.json()["detail"]
    assert appels == []


def test_paire_en_minuscules_acceptee(client):
    assert client.get("/graphique/btcusdt?interval=1h").status_code == 200


def test_pas_de_temps_hors_liste_refuse(client):
    assert client.get("/graphique/BTCUSDT?interval=1m").status_code == 422
    assert client.get("/ordre/BTCUSDT?style=kamikaze").status_code == 422


def test_prediction_paire_inconnue_refusee(client):
    reponse = client.post("/prediction", json={"symbole": "ABCDEUSDT"})
    assert reponse.status_code == 404


# ---------------------------------------------------------------------------
#  Metriques
# ---------------------------------------------------------------------------

def test_metriques_exposees_pour_prometheus(client):
    client.post("/prediction", json={"symbole": "BTCUSDT", "style": "agressif"})
    texte = client.get("/metrics").text
    assert "cryptobot_decisions_total" in texte
    assert "cryptobot_appels_authentifies_total" in texte
    assert "http_request_duration" in texte


def test_refus_comptes_dans_les_metriques(client, anonyme):
    anonyme.get("/modele")
    texte = client.get("/metrics").text
    assert 'cryptobot_appels_refuses_total{raison="cle_absente"}' in texte


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


# ---------------------------------------------------------------------------
#  Ordre projete : take profit, stop loss et pronostic
# ---------------------------------------------------------------------------

class PipelineFactice:
    """Modele a barrieres : renvoie les trois probabilites dans l'ordre -1, 0, 1."""

    classes_ = np.array([-1, 0, 1])

    def __init__(self, probabilites=(0.25, 0.35, 0.40)):
        self.probabilites = probabilites

    def predict_proba(self, X):
        return np.tile(self.probabilites, (len(X), 1))


@pytest.fixture
def client_avec_barrieres(client, monkeypatch):
    monkeypatch.setattr(module_ordre, "charger_barrieres", lambda: {
        "pipeline": PipelineFactice(), "colonnes": [], "largeur_barrieres": 3.0,
        "horizon": 12, "entraine_le": "2026-09-18T15:48:00+00:00",
    })
    return client


def test_ordre_place_les_niveaux_autour_du_prix(client_avec_barrieres):
    corps = client_avec_barrieres.get("/ordre/BTCUSDT?interval=1h&style=agressif").json()
    ordre = corps["ordre"]
    assert ordre["take_profit"] > ordre["entree"] > ordre["stop_loss"]   # configuration d'achat
    assert ordre["distance_pct"] > 0
    assert ordre["largeur_sigma"] == 3.0
    assert ordre["horizon_bougies"] == 12
    assert ordre["ratio_gain_risque"] == 1.0


def test_ordre_deduit_les_frais_du_gain_affiche(client_avec_barrieres):
    """Le gain annonce doit etre celui qu'on touche, frais deduits."""
    ordre = client_avec_barrieres.get("/ordre/BTCUSDT").json()["ordre"]
    assert ordre["gain_net_si_take_profit_pct"] == pytest.approx(ordre["distance_pct"] - 0.2, abs=1e-6)
    assert ordre["perte_nette_si_stop_loss_pct"] == pytest.approx(-ordre["distance_pct"] - 0.2, abs=1e-6)


def test_ordre_donne_le_pronostic_du_modele_a_barrieres(client_avec_barrieres):
    ordre = client_avec_barrieres.get("/ordre/BTCUSDT").json()["ordre"]
    assert ordre["pronostic_sens"] == 1                 # 0,40 est la plus forte
    assert sum(ordre["probabilites"].values()) == pytest.approx(1.0, abs=1e-6)
    assert set(ordre["probabilites"]) == {"stop loss touche en premier",
                                          "aucune barriere touchee",
                                          "take profit touche en premier"}


def test_ordre_sans_modele_a_barrieres_donne_503(client, monkeypatch):
    def absent():
        raise module_ordre.ModeleBarrieresIndisponible("modele introuvable")

    monkeypatch.setattr(module_ordre, "charger_barrieres", absent)
    assert client.get("/ordre/BTCUSDT").status_code == 503


def test_graphique_separe_la_bougie_en_cours(client, monkeypatch):
    """La bougie en cours est affichable mais jamais predite.

    Sa cloture n'existe pas encore : la faire passer dans le modele
    reviendrait a lui donner un prix intermediaire pour un prix de cloture.
    """
    bougies = bougies_factices()
    maintenant = pd.Timestamp.now(tz="UTC")
    # On fabrique une bougie 1h encore ouverte.
    en_cours = bougies[bougies["interval"] == "1h"].tail(1).copy()
    en_cours["open_time"] = maintenant.floor("h")
    en_cours["close_time"] = maintenant.floor("h") + pd.Timedelta("1h")
    monkeypatch.setattr(donnees, "bougies_binance",
                        lambda symbole, par_intervalle=300, inclure_en_cours=False:
                        pd.concat([bougies, en_cours], ignore_index=True))

    corps = client.get("/graphique/BTCUSDT?interval=1h&limite=30").json()
    assert corps["bougie_en_cours"] is not None
    assert pd.Timestamp(corps["bougie_en_cours"]["close_time"]) > maintenant
    # Aucune decision ne porte sur elle : elle n'est pas dans la serie predite.
    horodatages = {b["open_time"] for b in corps["donnees"]}
    assert corps["bougie_en_cours"]["open_time"] not in horodatages


# ---------------------------------------------------------------------------
#  Carnet de positions virtuelles
# ---------------------------------------------------------------------------

def test_positions_ouvre_puis_rend_le_bilan(client_avec_barrieres, monkeypatch):
    """Le carnet s'ouvre sur signal et rend un etat lisible."""
    appels = {}

    def synchroniser_factice(bougies, symbole, interval, style, decision, ordre):
        appels["decision"] = decision["decision"]
        return {"positions_fermees": 0, "position_ouverte": True}

    monkeypatch.setattr(module_positions, "bougies_a_rattraper", lambda *a: 0)
    monkeypatch.setattr(module_positions, "synchroniser", synchroniser_factice)
    monkeypatch.setattr(module_positions, "etat", lambda *a, **k: {
        "ouvertes": [{"sens": 1, "prix_entree": 100.0, "take_profit": 103.0,
                      "stop_loss": 97.0, "gain_en_cours_net_pct": 0.8,
                      "ouverte_sur_la_bougie": "2026-09-18T10:00:00+00:00",
                      "echeance": "2026-09-18T22:00:00+00:00", "prix_actuel": 100.9}],
        "historique": [], "bilan": {"positions_fermees": 0, "capital_simule": 10000.0}})

    reponse = client_avec_barrieres.post("/positions/BTCUSDT?interval=1h&style=agressif")
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["mouvement"]["position_ouverte"] is True
    assert corps["ouvertes"][0]["gain_en_cours_net_pct"] == 0.8
    assert appels["decision"] in {"acheter", "vendre", "attendre"}


def test_positions_carnet_indisponible_donne_503(client_avec_barrieres, monkeypatch):
    def tombe(*args, **kwargs):
        raise ConnectionError("base eteinte")

    monkeypatch.setattr(module_positions, "bougies_a_rattraper", lambda *a: 0)
    monkeypatch.setattr(module_positions, "synchroniser", tombe)
    assert client_avec_barrieres.post("/positions/BTCUSDT").status_code == 503


# ---------------------------------------------------------------------------
#  Rattrapage du carnet
# ---------------------------------------------------------------------------

def _serie(prix: list[tuple[float, float, float]], interval: str = "1h") -> pd.DataFrame:
    """Bougies (haut, bas, cloture) consecutives, pour piloter chaque scenario."""
    temps = pd.date_range("2026-09-18", periods=len(prix), freq="1h", tz="UTC")
    return pd.DataFrame({
        "open_time": temps, "close_time": temps + pd.Timedelta("1h"),
        "high": [p[0] for p in prix], "low": [p[1] for p in prix],
        "close": [p[2] for p in prix],
    })


def _decisions(serie: pd.DataFrame, signaux: dict[int, str]) -> dict:
    return {serie["open_time"].iloc[i].isoformat():
            {"decision": d, "probabilite_hausse": 0.6} for i, d in signaux.items()}


def test_simuler_take_profit_puis_nouvelle_position():
    """Achat a 100, barriere a +/-2 % : le haut a 103 touche l'objectif."""
    serie = _serie([(100, 100, 100), (101, 99.5, 100.5), (103, 100, 102.5),
                    (103, 102, 102.5), (103, 102, 102.5)])
    volatilite = np.full(len(serie), 0.01)            # 2 sigma = 2 %
    positions = module_positions.simuler(
        serie, _decisions(serie, {0: "acheter", 3: "vendre"}), volatilite,
        largeur=2.0, horizon=12, interval="1h", symbole="BTCUSDT", style="agressif",
        garder_ouverte=True)

    assert positions[0]["statut"] == "take profit"
    assert positions[0]["rendement_brut_pct"] == pytest.approx(2.0)
    # Frais de 0,2 % deduits du gain net.
    assert positions[0]["rendement_net_pct"] == pytest.approx(1.8)
    # La vente de la bougie 3 n'a pas pu se solder : elle reste ouverte.
    assert positions[1]["statut"] == "ouverte" and positions[1]["sens"] == -1


def test_simuler_stop_loss_prioritaire_dans_la_meme_bougie():
    """Les deux barrieres dans une bougie : l'hypothese prudente l'emporte."""
    serie = _serie([(100, 100, 100), (103, 97, 100)])
    positions = module_positions.simuler(
        serie, _decisions(serie, {0: "acheter"}), np.full(2, 0.01), 2.0, 12,
        "1h", "BTCUSDT", "agressif")
    assert positions[0]["statut"] == "stop loss"


def test_simuler_une_seule_position_a_la_fois():
    """Un signal pendant qu'une position court est ignore."""
    serie = _serie([(100, 100, 100)] + [(100.5, 99.5, 100)] * 4 + [(103, 100, 102)])
    positions = module_positions.simuler(
        serie, _decisions(serie, {0: "acheter", 2: "vendre", 3: "vendre"}),
        np.full(len(serie), 0.01), 2.0, 12, "1h", "BTCUSDT", "agressif")
    assert len(positions) == 1 and positions[0]["sens"] == 1


def test_simuler_respecte_le_point_de_depart():
    """Le rattrapage ne rejoue pas les bougies deja evaluees."""
    serie = _serie([(100, 100, 100), (100, 100, 100), (100, 100, 100), (103, 100, 102)])
    positions = module_positions.simuler(
        serie, _decisions(serie, {0: "acheter", 2: "acheter"}), np.full(4, 0.01),
        2.0, 12, "1h", "BTCUSDT", "agressif", debut=1)
    assert [p["bougie_signal"] for p in positions] == [serie["open_time"].iloc[2]]


def test_positions_rattrape_avant_le_suivi(client_avec_barrieres, monkeypatch):
    """Des bougies manquees sont rejouees, et le resultat est rendu."""
    ordre_des_appels = []
    monkeypatch.setattr(module_positions, "bougies_a_rattraper", lambda *a: 72)
    monkeypatch.setattr(module_positions, "rattraper", lambda *a, **k: (
        ordre_des_appels.append("rattraper")
        or {"bougies_rejouees": 72, "positions_ajoutees": 3, "positions_fermees": 1}))
    monkeypatch.setattr(module_positions, "synchroniser", lambda *a, **k: (
        ordre_des_appels.append("synchroniser")
        or {"positions_fermees": 0, "position_ouverte": False}))
    monkeypatch.setattr(module_positions, "etat", lambda *a, **k: {
        "ouvertes": [], "historique": [], "bilan": {}})

    corps = client_avec_barrieres.post("/positions/BTCUSDT?interval=1h").json()
    assert ordre_des_appels == ["rattraper", "synchroniser"]
    assert corps["rattrapage"]["positions_ajoutees"] == 3


def test_positions_rattrapage_en_echec_naffecte_pas_le_carnet(client_avec_barrieres, monkeypatch):
    """Un rattrapage qui echoue est journalise ; le carnet s'affiche quand meme."""
    def tombe(*args, **kwargs):
        raise ConnectionError("base eteinte")

    monkeypatch.setattr(module_positions, "bougies_a_rattraper", tombe)
    monkeypatch.setattr(module_positions, "synchroniser", lambda *a, **k: {
        "positions_fermees": 0, "position_ouverte": False})
    monkeypatch.setattr(module_positions, "etat", lambda *a, **k: {
        "ouvertes": [], "historique": [], "bilan": {}})

    reponse = client_avec_barrieres.post("/positions/BTCUSDT")
    assert reponse.status_code == 200 and reponse.json()["rattrapage"] is None
