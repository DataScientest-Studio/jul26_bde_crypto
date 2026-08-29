"""Client REST Binance - recuperation generique de donnees de marche.

Principe directeur : AUCUNE fonction ne connait "BTCUSDT". Le symbole,
l'intervalle et la fenetre temporelle sont toujours des parametres. Ajouter
une 6e paire = une ligne dans config.PAIRES, zero ligne de code.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import requests

from . import config

logger = logging.getLogger(__name__)


def vers_ms(date: str | int | datetime) -> int:
    """Convertit une date en timestamp milliseconde UTC (format attendu par Binance)."""
    if isinstance(date, int):
        return date
    if isinstance(date, str):
        date = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return int(date.timestamp() * 1000)


class ClientBinance:
    """Client REST avec gestion du quota, des erreurs et bascule sur miroir.

    On instancie une seule fois et on reutilise : `requests.Session` garde la
    connexion TCP/TLS ouverte, ce qui reduit fortement la latence sur des
    milliers d'appels successifs.
    """

    def __init__(self, base: str = config.BASE_REST, timeout: int = 15):
        self.base = base
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "jul26_bde_crypto/etape1"})
        self.poids_utilise = 0

    # -- couche bas niveau --------------------------------------------------
    def _get(self, chemin: str, params: dict | None = None, tentatives: int = 4):
        """GET avec backoff exponentiel, respect du quota et bascule sur miroir.

        Codes Binance a connaitre :
          429 = quota depasse, il faut ralentir
          418 = IP bannie temporairement (on a ignore un 429 de trop)
          5xx = probleme cote Binance
        """
        for tentative in range(tentatives):
            self._respecter_quota()
            try:
                r = self.session.get(f"{self.base}{chemin}", params=params, timeout=self.timeout)
            except requests.RequestException as e:
                attente = 2 ** tentative
                logger.warning("Erreur reseau (%s), nouvel essai dans %ss", type(e).__name__, attente)
                time.sleep(attente)
                self._basculer_miroir()
                continue

            # Binance publie le quota consomme a chaque reponse : on le lit
            # plutot que de compter nos requetes nous-memes (source de verite).
            entete = r.headers.get("x-mbx-used-weight-1m")
            if entete:
                self.poids_utilise = int(entete)

            if r.status_code == 200:
                return r.json()

            if r.status_code in (429, 418):
                attente = int(r.headers.get("Retry-After", 2 ** (tentative + 4)))
                logger.warning("Quota atteint (HTTP %s). Pause de %ss.", r.status_code, attente)
                time.sleep(attente)
                continue

            if 500 <= r.status_code < 600:
                attente = 2 ** tentative
                logger.warning("Erreur serveur %s, nouvel essai dans %ss", r.status_code, attente)
                time.sleep(attente)
                self._basculer_miroir()
                continue

            # 4xx autre que quota : la requete est fautive, reessayer ne sert a rien.
            raise RuntimeError(f"Requete refusee HTTP {r.status_code} : {r.text[:200]}")

        raise RuntimeError(f"Echec de {chemin} apres {tentatives} tentatives")

    def _respecter_quota(self):
        """Pause preventive avant de froler la limite (evite le ban 418)."""
        if self.poids_utilise >= config.SEUIL_POIDS_PAUSE:
            logger.info("Poids a %s/%s : pause 10s.", self.poids_utilise, config.LIMITE_POIDS_MINUTE)
            time.sleep(10)
            self.poids_utilise = 0

    def _basculer_miroir(self):
        """Alterne entre l'hote principal et le miroir public en cas de panne."""
        self.base = config.BASE_REST_REPLI if self.base == config.BASE_REST else config.BASE_REST
        logger.info("Bascule sur %s", self.base)

    # -- endpoints publics --------------------------------------------------
    def ping(self) -> bool:
        self._get("/api/v3/ping")
        return True

    def infos_marches(self, symboles: list[str] | None = None) -> dict:
        """Metadonnees des paires : statut, precisions, filtres de trading."""
        params = {}
        if symboles:
            # ATTENTION : Binance refuse (HTTP 400) le JSON contenant des espaces.
            params["symbols"] = json.dumps(symboles, separators=(",", ":"))
        return self._get("/api/v3/exchangeInfo", params)

    def klines(self, symbole: str, intervalle: str, debut=None, fin=None, limite: int = 1000) -> list[list]:
        """Un lot de bougies brutes (max 1000). Brique de base de la pagination."""
        params = {
            "symbol": symbole,
            "interval": intervalle,
            "limit": min(limite, config.KLINES_MAX_PAR_REQUETE),
        }
        if debut is not None:
            params["startTime"] = vers_ms(debut)
        if fin is not None:
            params["endTime"] = vers_ms(fin)
        return self._get("/api/v3/klines", params)

    def klines_historique(self, symbole: str, intervalle: str, debut, fin=None) -> list[list]:
        """Historique COMPLET sur une periode, en enchainant les lots de 1000.

        Point critique : on avance sur le timestamp d'ouverture de la derniere
        bougie recue (+1 ms), jamais sur un compteur de boucle. Binance tronque
        silencieusement a 1000 resultats ; un compteur supposerait le lot plein
        et sauterait des donnees sans jamais lever d'erreur.
        """
        debut_ms = vers_ms(debut)
        fin_ms = vers_ms(fin) if fin is not None else int(time.time() * 1000)
        toutes, curseur = [], debut_ms

        while curseur < fin_ms:
            lot = self.klines(symbole, intervalle, debut=curseur, fin=fin_ms)
            if not lot:
                break  # plus rien a lire : on a rattrape le present
            toutes.extend(lot)
            dernier_debut = lot[-1][0]
            if dernier_debut <= curseur:
                break  # securite anti-boucle infinie
            curseur = dernier_debut + 1

        logger.info("%s %s : %d bougies recuperees (poids consomme %s)",
                    symbole, intervalle, len(toutes), self.poids_utilise)
        return toutes

    def ticker_24h(self, symboles: list[str] | None = None):
        params = {}
        if symboles:
            params["symbols"] = json.dumps(symboles, separators=(",", ":"))
        return self._get("/api/v3/ticker/24hr", params)

    def carnet_ordres(self, symbole: str, profondeur: int = 100) -> dict:
        return self._get("/api/v3/depth", {"symbol": symbole, "limit": profondeur})
