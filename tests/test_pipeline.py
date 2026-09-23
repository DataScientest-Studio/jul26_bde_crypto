"""Tests de l'etape 5 : pipeline automatise, supervision et deploiement.

Meme principe que tests/test_api.py : aucune base, aucun appel reseau. On
teste les DECISIONS (faut-il alerter, promouvoir, ecarter une bougie ?), qui
sont des fonctions pures ; les acces aux bases sont verifies par les
controles de deploiement de la CI (scripts/verifier_deploiement.py).

Lancement :
    pytest tests/test_pipeline.py -v
"""
from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path

import joblib
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import exportateur, modele
from scripts import controle_qualite, generer_secrets, ingestion_continue, reentrainer

RACINE = Path(__file__).resolve().parent.parent
MAINTENANT = pd.Timestamp("2026-09-23 12:00", tz="UTC")


def serie_15m(debut: str, n: int, **modifs) -> pd.DataFrame:
    temps = pd.date_range(debut, periods=n, freq="15min", tz="UTC")
    df = pd.DataFrame({"open_time": temps, "open": 100.0, "high": 101.0, "low": 99.0,
                       "close": 100.5, "volume": 10.0})
    for colonne, valeurs in modifs.items():
        df[colonne] = valeurs
    return df


# ---------------------------------------------------------------------------
#  Controle qualite
# ---------------------------------------------------------------------------

def test_controle_serie_complete_et_fraiche():
    # 48 bougies jusqu'a 11:30, cloturee a 11:45 : 15 min de retard a midi.
    serie = serie_15m("2026-09-23 00:00", 47)
    resultat = controle_qualite.controler(serie, "15m", MAINTENANT - pd.Timedelta(hours=12),
                                          MAINTENANT)
    assert resultat["statut"] == "ok"
    assert resultat["bougies_manquantes"] == 0
    assert resultat["retard_minutes"] == 15.0


def test_controle_detecte_les_bougies_manquantes():
    serie = serie_15m("2026-09-23 00:00", 47).drop(index=[10, 11, 12])
    resultat = controle_qualite.controler(serie, "15m", MAINTENANT - pd.Timedelta(hours=12),
                                          MAINTENANT)
    assert resultat["bougies_manquantes"] == 3
    assert resultat["statut"] == "alerte"


def test_controle_detecte_une_bougie_incoherente():
    """Un haut sous le bas ne peut pas exister : c'est un echec, pas une alerte."""
    hauts = [101.0] * 47
    hauts[5] = 98.0
    serie = serie_15m("2026-09-23 00:00", 47, high=hauts)
    resultat = controle_qualite.controler(serie, "15m", MAINTENANT - pd.Timedelta(hours=12),
                                          MAINTENANT)
    assert resultat["bougies_incoherentes"] == 1
    assert resultat["statut"] == "echec"


def test_controle_collecte_arretee():
    """Plus rien depuis 6 heures en 15 min : la collecte est tombee."""
    serie = serie_15m("2026-09-23 00:00", 24)
    resultat = controle_qualite.controler(serie, "15m", MAINTENANT - pd.Timedelta(hours=12),
                                          MAINTENANT)
    assert resultat["statut"] == "echec"


def test_controle_serie_vide_est_un_echec():
    resultat = controle_qualite.controler(serie_15m("2026-09-23", 0), "15m",
                                          MAINTENANT - pd.Timedelta(days=2), MAINTENANT)
    assert resultat["statut"] == "echec"


# ---------------------------------------------------------------------------
#  Ingestion continue
# ---------------------------------------------------------------------------

def test_la_bougie_en_cours_n_est_jamais_ecrite():
    """Son prix de cloture n'est qu'un prix intermediaire."""
    maintenant_ms = 1_000_000
    brut = [[0, "1", "1", "1", "1", "1", maintenant_ms - 1],     # cloturee
            [1, "1", "1", "1", "1", "1", maintenant_ms + 60_000]]  # en cours
    assert ingestion_continue.garder_cloturees(brut, maintenant_ms) == [brut[0]]


def test_la_collecte_reprend_a_la_bougie_suivante():
    derniere = pd.Timestamp("2026-09-23 09:00", tz="UTC")
    assert ingestion_continue.debut_de_collecte(derniere, "15m") == pd.Timestamp(
        "2026-09-23 09:15", tz="UTC")
    assert ingestion_continue.debut_de_collecte(derniere, "4h") == pd.Timestamp(
        "2026-09-23 13:00", tz="UTC")


def test_base_vide_tout_l_historique_du_profil():
    debut = ingestion_continue.debut_de_collecte(None, "1h")
    assert pd.Timestamp.now(tz="UTC") - debut > pd.Timedelta(days=700)


# ---------------------------------------------------------------------------
#  Reentrainement : la regle de promotion
# ---------------------------------------------------------------------------

def test_challenger_meilleur_promu():
    decision, _ = reentrainer.decider_promotion({"ordres": 400, "accuracy": 0.56},
                                                {"ordres": 380, "accuracy": 0.58})
    assert decision == "promu"


def test_egalite_promu():
    """A qualite egale, le modele le plus recent connait le marche le plus recent."""
    decision, _ = reentrainer.decider_promotion({"ordres": 400, "accuracy": 0.57},
                                                {"ordres": 400, "accuracy": 0.57})
    assert decision == "promu"


def test_challenger_moins_bon_rejete():
    decision, raison = reentrainer.decider_promotion({"ordres": 447, "accuracy": 0.5749},
                                                     {"ordres": 539, "accuracy": 0.5584})
    assert decision == "rejete"
    assert "on garde" in raison


def test_trop_peu_d_ordres_pour_juger():
    """10 ordres a 70 % ne prouvent rien : l'intervalle de confiance est enorme."""
    decision, raison = reentrainer.decider_promotion({"ordres": 400, "accuracy": 0.55},
                                                     {"ordres": 10, "accuracy": 0.70})
    assert decision == "rejete"
    assert "necessaires" in raison


def test_sans_champion_le_challenger_prend_la_place():
    assert reentrainer.decider_promotion(None, {"ordres": 100, "accuracy": 0.51})[0] == "promu"
    assert reentrainer.decider_promotion({"ordres": 0}, {"ordres": 100, "accuracy": 0.51})[0] == "promu"


def test_remplacement_atomique(tmp_path):
    """Jamais de fichier a moitie ecrit, ni de fichier temporaire oublie."""
    source, cible = tmp_path / "candidat.joblib", tmp_path / "direction.joblib"
    source.write_bytes(b"nouveau modele")
    cible.write_bytes(b"ancien modele")
    reentrainer.remplacer_atomiquement(source, cible)
    assert cible.read_bytes() == b"nouveau modele"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["candidat.joblib", "direction.joblib"]


# ---------------------------------------------------------------------------
#  Rechargement a chaud du modele
# ---------------------------------------------------------------------------

def test_l_api_recharge_un_modele_remplace(tmp_path, monkeypatch):
    """Un modele promu par Airflow entre en service sans redemarrer l'API."""
    chemin = tmp_path / "direction.joblib"
    paquet = {"modele": None, "colonnes": ["a"], "styles": {}, "version": "v1"}
    joblib.dump(paquet, chemin)
    monkeypatch.setattr(modele, "CHEMIN_MODELE", chemin)
    monkeypatch.setattr(modele, "_EN_MEMOIRE", {"signature": None, "paquet": None})

    assert modele.charger()["version"] == "v1"
    assert modele.charger() is modele.charger()          # pas relu a chaque appel

    joblib.dump({**paquet, "version": "v2", "colonnes": ["a", "b"]}, chemin)
    futur = time.time() + 10
    os.utime(chemin, (futur, futur))
    assert modele.charger()["version"] == "v2"


# ---------------------------------------------------------------------------
#  Exportateur de metriques
# ---------------------------------------------------------------------------

def test_exportateur_traduit_la_base_en_metriques(monkeypatch):
    collecteur = exportateur.CollecteurPipeline()
    monkeypatch.setattr(collecteur, "lire", lambda: {
        "retard": [("BTCUSDT", "15m", 540.0)],
        "qualite": [("BTCUSDT", "15m", "alerte", 2)],
        "derive": [("BTCUSDT", "15m", 0.04, 0.4, 3, 1_758_600_000.0)],
        "bot": [("15m", "agressif", 120.0)],
        "carnet": [("15m", "agressif", 1, 40, 0.52, -0.03)],
        "reentrainement": [(1_758_600_000.0, False, 0.558, 0.575)],
    })
    valeurs = {(m.name, tuple(sorted(s.labels.items()))): s.value
               for m in collecteur.collect() for s in m.samples}
    assert valeurs[("cryptobot_donnees_retard_secondes",
                    (("interval", "15m"), ("symbol", "BTCUSDT")))] == 540.0
    assert valeurs[("cryptobot_controle_qualite_statut",
                    (("interval", "15m"), ("symbol", "BTCUSDT")))] == 1
    assert valeurs[("cryptobot_reentrainement_dernier_promu", ())] == 0.0
    assert valeurs[("cryptobot_base_joignable", ())] == 1.0


def test_exportateur_base_injoignable(monkeypatch):
    """Base tombee : l'exportateur le DIT (metrique a 0) au lieu de planter."""
    collecteur = exportateur.CollecteurPipeline()
    monkeypatch.setattr(collecteur, "lire", lambda: {})
    base = next(m for m in collecteur.collect() if m.name == "cryptobot_base_joignable")
    assert base.samples[0].value == 0.0


# ---------------------------------------------------------------------------
#  Secrets
# ---------------------------------------------------------------------------

MODELE_ENV = """# commentaire
CRYPTOBOT_CLE_INTERFACE=__A_GENERER__
AIRFLOW_FERNET_KEY=__A_GENERER_FERNET__
POSTGRES_USER=cryptobot
"""


def test_secrets_generes_une_seule_fois():
    contenu, crees = generer_secrets.completer(MODELE_ENV, {})
    assert crees == ["CRYPTOBOT_CLE_INTERFACE", "AIRFLOW_FERNET_KEY"]
    valeurs = dict(l.split("=", 1) for l in contenu.splitlines() if "=" in l and not l.startswith("#"))
    assert len(valeurs["CRYPTOBOT_CLE_INTERFACE"]) == 64
    assert len(base64.urlsafe_b64decode(valeurs["AIRFLOW_FERNET_KEY"])) == 32

    # Deuxieme passage : rien ne change, sinon tout ce qui utilise deja les
    # secrets (base Airflow, clients de l'API) serait casse.
    second, crees = generer_secrets.completer(MODELE_ENV, valeurs)
    assert crees == []
    assert second == contenu


def test_variables_locales_conservees():
    contenu, _ = generer_secrets.completer(MODELE_ENV, {"MA_VARIABLE": "42"})
    assert "MA_VARIABLE=42" in contenu


def test_env_n_est_pas_versionne():
    assert ".env" in (RACINE / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in (RACINE / ".dockerignore").read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
#  Interface et proxy (configuration)
# ---------------------------------------------------------------------------

def test_la_page_ne_contient_aucune_cle():
    page = (RACINE / "interface" / "index.html").read_text(encoding="utf-8")
    assert "X-API-Key" not in page
    assert 'const API = "/api"' in page


def test_le_proxy_injecte_la_cle_et_cache_les_metriques():
    conf = (RACINE / "interface" / "nginx.conf.template").read_text(encoding="utf-8")
    proxy = (RACINE / "interface" / "snippets" / "proxy_api.conf").read_text(encoding="utf-8")
    assert 'proxy_set_header X-API-Key "${CRYPTOBOT_CLE_INTERFACE}";' in proxy
    assert "location = /api/metrics {\n        return 404;" in conf
    assert "limit_req_zone" in conf
    assert "server_tokens off;" in conf


def test_aucun_port_publie_pour_l_api():
    """L'API n'est joignable que par le proxy, Airflow et Prometheus."""
    import yaml

    services = yaml.safe_load((RACINE / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    assert "ports" not in services["api"]
    assert services["interface"]["networks"] == ["services"]
    for nom, service in services.items():
        for port in service.get("ports", []):
            assert port.startswith(("127.0.0.1:", "${INTERFACE_ECOUTE")), f"{nom} publie {port}"
