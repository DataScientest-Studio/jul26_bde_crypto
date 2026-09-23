"""Exportateur Prometheus : l'etat du pipeline, lu dans la base.

L'API expose ses propres metriques (appels, durees, decisions). Mais les
questions les plus importantes en production ne concernent pas l'API :

    les donnees arrivent-elles encore ?          retard de la derniere bougie
    le bot tourne-t-il ?                          age du dernier tour de carnet
    le modele est-il encore adapte au marche ?    derive (PSI) par paire
    le reentrainement a-t-il lieu ?               date et issue du dernier duel
    que rapporte le bot ?                         trades, taux de gagnants

Les reponses sont en base (ecrites par Airflow et par l'API). Ce petit
service les traduit en metriques, pour que Prometheus puisse declencher des
ALERTES dessus (voir monitoring/prometheus/alertes.yml) - Grafana seul ne
ferait qu'afficher.

Service a part, meme image que l'API : il tourne meme si l'API tombe, et
l'API ne paie pas le cout de ces requetes. Les requetes sont mises en cache
60 secondes : Prometheus lit toutes les minutes, la base n'est jamais
sollicitee plus souvent.

Lancement :  python -m api.exportateur          (port 9101)
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prometheus_client import REGISTRY, Counter, start_http_server
from prometheus_client.core import GaugeMetricFamily

from src import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("exportateur")

PORT = int(os.getenv("CRYPTOBOT_EXPORTATEUR_PORT", "9101"))
CACHE_SECONDES = 60
MODELE = Path(os.getenv("CRYPTOBOT_MODELE", config.ROOT / "models" / "direction_day_trading.joblib"))
STATUTS = {"ok": 0, "alerte": 1, "echec": 2}

ERREURS = Counter("cryptobot_exportateur_erreurs_total",
                  "Lectures de la base en echec pendant une collecte de metriques")

REQUETES = {
    "retard": """
        SELECT symbol, interval,
               EXTRACT(EPOCH FROM now() - (max(open_time) + CASE interval
                   WHEN '15m' THEN interval '15 minutes'
                   WHEN '1h'  THEN interval '1 hour'
                   ELSE interval '4 hours' END))
          FROM v_day_trading
         WHERE open_time > now() - interval '30 days'
         GROUP BY symbol, interval""",
    "qualite": """
        SELECT DISTINCT ON (symbol, interval) symbol, interval, statut, bougies_manquantes
          FROM controles_qualite ORDER BY symbol, interval, controle_le DESC""",
    "derive": """
        SELECT symbol, interval, psi_median, psi_maximum, variables_en_derive_forte,
               EXTRACT(EPOCH FROM mesure_le)
          FROM derive_mesures
         WHERE mesure_le = (SELECT max(mesure_le) FROM derive_mesures)""",
    "bot": """
        SELECT interval, style, EXTRACT(EPOCH FROM now() - max(mis_a_jour))
          FROM carnet_suivi GROUP BY interval, style""",
    "carnet": """
        SELECT interval, style,
               count(*) FILTER (WHERE statut = 'ouverte'),
               count(*) FILTER (WHERE statut <> 'ouverte' AND fermee_a > now() - interval '7 days'),
               avg((rendement_net_pct > 0)::int) FILTER (
                   WHERE statut <> 'ouverte' AND fermee_a > now() - interval '30 days'),
               avg(rendement_net_pct) FILTER (
                   WHERE statut <> 'ouverte' AND fermee_a > now() - interval '30 days')
          FROM positions_virtuelles GROUP BY interval, style""",
    "reentrainement": """
        SELECT EXTRACT(EPOCH FROM lance_le), decision = 'promu',
               challenger_accuracy, champion_accuracy
          FROM reentrainements ORDER BY lance_le DESC LIMIT 1""",
}


class CollecteurPipeline:
    """Collecteur Prometheus : lu a chaque passage de Prometheus."""

    def __init__(self):
        self._cache: tuple[float, dict] = (0.0, {})

    def lire(self) -> dict:
        moment, resultats = self._cache
        if time.time() - moment < CACHE_SECONDES:
            return resultats
        from src.database import postgres_connection

        resultats = {}
        try:
            with postgres_connection(autocommit=True) as conn, conn.cursor() as cur:
                for nom, requete in REQUETES.items():
                    try:
                        cur.execute(requete)
                        resultats[nom] = cur.fetchall()
                    except Exception as exc:
                        # Une table absente (base pas encore migree) ne doit
                        # pas priver Prometheus des autres metriques.
                        ERREURS.inc()
                        log.warning("Requete %s en echec : %s", nom, exc)
        except Exception as exc:
            ERREURS.inc()
            log.warning("Base injoignable : %s", exc)
        self._cache = (time.time(), resultats)
        return resultats

    def collect(self):
        donnees = self.lire()

        base = GaugeMetricFamily("cryptobot_base_joignable",
                                 "1 si l'exportateur a pu lire la base a sa derniere collecte")
        base.add_metric([], 1.0 if donnees else 0.0)
        yield base

        retard = GaugeMetricFamily(
            "cryptobot_donnees_retard_secondes",
            "Temps ecoule depuis la cloture de la derniere bougie en base",
            labels=["symbol", "interval"])
        for symbole, interval, secondes in donnees.get("retard", []):
            retard.add_metric([symbole, interval], float(secondes))
        yield retard

        qualite = GaugeMetricFamily(
            "cryptobot_controle_qualite_statut",
            "Dernier controle qualite : 0 ok, 1 alerte, 2 echec", labels=["symbol", "interval"])
        manquantes = GaugeMetricFamily(
            "cryptobot_controle_qualite_bougies_manquantes",
            "Bougies manquantes au dernier controle", labels=["symbol", "interval"])
        for symbole, interval, statut, nb in donnees.get("qualite", []):
            qualite.add_metric([symbole, interval], STATUTS.get(statut, 2))
            manquantes.add_metric([symbole, interval], nb)
        yield qualite
        yield manquantes

        psi_median = GaugeMetricFamily("cryptobot_derive_psi_median",
                                       "PSI median a la derniere mesure de derive",
                                       labels=["symbol", "interval"])
        psi_max = GaugeMetricFamily("cryptobot_derive_psi_maximum",
                                    "PSI maximum a la derniere mesure de derive",
                                    labels=["symbol", "interval"])
        fortes = GaugeMetricFamily("cryptobot_derive_variables_fortes",
                                   "Variables en derive forte (PSI > 0,25)",
                                   labels=["symbol", "interval"])
        mesure = GaugeMetricFamily("cryptobot_derive_mesuree_timestamp_seconds",
                                   "Moment de la derniere mesure de derive")
        derniere = 0.0
        for symbole, interval, med, mx, nb, quand in donnees.get("derive", []):
            psi_median.add_metric([symbole, interval], float(med or 0))
            psi_max.add_metric([symbole, interval], float(mx or 0))
            fortes.add_metric([symbole, interval], nb)
            derniere = max(derniere, float(quand))
        if derniere:
            mesure.add_metric([], derniere)
        yield from (psi_median, psi_max, fortes, mesure)

        bot = GaugeMetricFamily("cryptobot_bot_dernier_tour_age_secondes",
                                "Temps ecoule depuis le dernier tour du carnet",
                                labels=["interval", "style"])
        for interval, style, secondes in donnees.get("bot", []):
            bot.add_metric([interval, style], float(secondes))
        yield bot

        ouvertes = GaugeMetricFamily("cryptobot_carnet_positions_ouvertes",
                                     "Positions virtuelles en cours", labels=["interval", "style"])
        trades = GaugeMetricFamily("cryptobot_carnet_trades_7_jours",
                                   "Trades clotures sur 7 jours", labels=["interval", "style"])
        gagnants = GaugeMetricFamily("cryptobot_carnet_taux_gagnants_30_jours",
                                     "Part des trades gagnants (frais deduits) sur 30 jours",
                                     labels=["interval", "style"])
        gain = GaugeMetricFamily("cryptobot_carnet_gain_net_moyen_30_jours_pct",
                                 "Gain net moyen par trade sur 30 jours, en %",
                                 labels=["interval", "style"])
        for interval, style, nb_ouvertes, nb_trades, taux, moyen in donnees.get("carnet", []):
            ouvertes.add_metric([interval, style], nb_ouvertes)
            trades.add_metric([interval, style], nb_trades)
            if taux is not None:
                gagnants.add_metric([interval, style], float(taux))
                gain.add_metric([interval, style], float(moyen))
        yield from (ouvertes, trades, gagnants, gain)

        dernier = GaugeMetricFamily("cryptobot_reentrainement_dernier_timestamp_seconds",
                                    "Moment du dernier duel champion / challenger")
        promu = GaugeMetricFamily("cryptobot_reentrainement_dernier_promu",
                                  "1 si le dernier challenger a ete promu, 0 sinon")
        for quand, est_promu, _challenger, _champion in donnees.get("reentrainement", []):
            dernier.add_metric([], float(quand))
            promu.add_metric([], 1.0 if est_promu else 0.0)
        yield dernier
        yield promu

        age = GaugeMetricFamily("cryptobot_modele_fichier_age_secondes",
                                "Age du fichier du modele en service (remplace a chaque promotion)")
        if MODELE.exists():
            age.add_metric([], time.time() - MODELE.stat().st_mtime)
        yield age


def main():
    REGISTRY.register(CollecteurPipeline())
    start_http_server(PORT)
    log.info("Exportateur en ecoute sur :%d/metrics", PORT)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
