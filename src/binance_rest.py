"""Client REST Binance - recuperation generique de donnees de marche.

Principe directeur : AUCUNE fonction ne connait "BTCUSDT". Le symbole,
l'intervalle et la fenetre temporelle sont toujours des parametres. Ajouter
une 6e paire = une ligne dans config.PAIRS, zero ligne de code.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from . import config

logger = logging.getLogger(__name__)


def to_millis(date: str | int | datetime) -> int:
    """Convertit une date en timestamp milliseconde UTC (format attendu par Binance)."""
    if isinstance(date, int):
        return date
    if isinstance(date, str):
        date = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return int(date.timestamp() * 1000)


def start_date_for(history_days: int | None) -> str:
    """Traduit une profondeur en jours vers une date de debut.

    `None` signifie "tout l'historique disponible" : on retombe alors sur la
    date plancher du projet, imposee par SOLUSDT.
    """
    if history_days is None:
        return config.HISTORY_START

    start = datetime.now(timezone.utc) - timedelta(days=history_days)
    # Jamais avant le plancher du projet : au-dela, certaines paires n'existent pas.
    floor = datetime.fromisoformat(config.HISTORY_START).replace(tzinfo=timezone.utc)
    return max(start, floor).strftime("%Y-%m-%d")


class BinanceClient:
    """Client REST avec gestion du quota, des erreurs et bascule sur miroir.

    On instancie une seule fois et on reutilise : `requests.Session` garde la
    connexion TCP/TLS ouverte, ce qui reduit fortement la latence sur des
    milliers d'appels successifs.
    """

    def __init__(self, base_url: str = config.REST_BASE_URL, timeout: int = 15):
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "jul26_bde_crypto/etape1"})
        self.used_weight = 0

    # -- couche bas niveau --------------------------------------------------
    def _get(self, path: str, params: dict | None = None, attempts: int = 4):
        """GET avec backoff exponentiel, respect du quota et bascule sur miroir.

        Codes Binance a connaitre :
          429 = quota depasse, il faut ralentir
          418 = IP bannie temporairement (on a ignore un 429 de trop)
          5xx = probleme cote Binance
        """
        for attempt in range(attempts):
            self._respect_quota()
            try:
                response = self.session.get(
                    f"{self.base_url}{path}", params=params, timeout=self.timeout
                )
            except requests.RequestException as exc:
                delay = 2 ** attempt
                logger.warning(
                    "Erreur reseau (%s), nouvel essai dans %ss", type(exc).__name__, delay
                )
                time.sleep(delay)
                self._switch_host()
                continue

            # Binance publie le quota consomme a chaque reponse : on le lit
            # plutot que de compter nos requetes nous-memes (source de verite).
            header = response.headers.get("x-mbx-used-weight-1m")
            if header:
                self.used_weight = int(header)

            if response.status_code == 200:
                return response.json()

            if response.status_code in (429, 418):
                delay = int(response.headers.get("Retry-After", 2 ** (attempt + 4)))
                logger.warning(
                    "Quota atteint (HTTP %s). Pause de %ss.", response.status_code, delay
                )
                time.sleep(delay)
                continue

            if 500 <= response.status_code < 600:
                delay = 2 ** attempt
                logger.warning(
                    "Erreur serveur %s, nouvel essai dans %ss", response.status_code, delay
                )
                time.sleep(delay)
                self._switch_host()
                continue

            # 4xx autre que quota : la requete est fautive, reessayer ne sert a rien.
            raise RuntimeError(
                f"Requete refusee HTTP {response.status_code} : {response.text[:200]}"
            )

        raise RuntimeError(f"Echec de {path} apres {attempts} tentatives")

    def _respect_quota(self):
        """Pause preventive avant de froler la limite (evite le ban 418)."""
        if self.used_weight >= config.WEIGHT_PAUSE_THRESHOLD:
            logger.info(
                "Poids a %s/%s : pause 10s.",
                self.used_weight,
                config.WEIGHT_LIMIT_PER_MINUTE,
            )
            time.sleep(10)
            self.used_weight = 0

    def _switch_host(self):
        """Alterne entre l'hote principal et le miroir public en cas de panne."""
        self.base_url = (
            config.REST_FALLBACK_URL
            if self.base_url == config.REST_BASE_URL
            else config.REST_BASE_URL
        )
        logger.info("Bascule sur %s", self.base_url)

    # -- endpoints publics --------------------------------------------------
    def ping(self) -> bool:
        self._get("/api/v3/ping")
        return True

    def exchange_info(self, symbols: list[str] | None = None) -> dict:
        """Metadonnees des paires : statut, precisions, filtres de trading."""
        params = {}
        if symbols:
            # ATTENTION : Binance refuse (HTTP 400) le JSON contenant des espaces.
            params["symbols"] = json.dumps(symbols, separators=(",", ":"))
        return self._get("/api/v3/exchangeInfo", params)

    def klines(
        self,
        symbol: str,
        interval: str,
        start=None,
        end=None,
        limit: int = 1000,
    ) -> list[list]:
        """Un lot de bougies brutes (max 1000). Brique de base de la pagination."""
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": min(limit, config.KLINES_MAX_PER_REQUEST),
        }
        if start is not None:
            params["startTime"] = to_millis(start)
        if end is not None:
            params["endTime"] = to_millis(end)
        return self._get("/api/v3/klines", params)

    def fetch_klines_history(
        self, symbol: str, interval: str, start, end=None
    ) -> list[list]:
        """Historique COMPLET sur une periode, en enchainant les lots de 1000.

        Point critique : on avance sur le timestamp d'ouverture de la derniere
        bougie recue (+1 ms), jamais sur un compteur de boucle. Binance tronque
        silencieusement a 1000 resultats ; un compteur supposerait le lot plein
        et sauterait des donnees sans jamais lever d'erreur.
        """
        start_ms = to_millis(start)
        end_ms = to_millis(end) if end is not None else int(time.time() * 1000)
        candles, cursor = [], start_ms

        while cursor < end_ms:
            batch = self.klines(symbol, interval, start=cursor, end=end_ms)
            if not batch:
                break  # plus rien a lire : on a rattrape le present
            candles.extend(batch)
            last_open_time = batch[-1][0]
            if last_open_time <= cursor:
                break  # securite anti-boucle infinie
            cursor = last_open_time + 1

        logger.info(
            "%s %s : %d bougies recuperees (poids consomme %s)",
            symbol,
            interval,
            len(candles),
            self.used_weight,
        )
        return candles

    def ticker_24h(self, symbols: list[str] | None = None):
        params = {}
        if symbols:
            params["symbols"] = json.dumps(symbols, separators=(",", ":"))
        return self._get("/api/v3/ticker/24hr", params)

    def order_book(self, symbol: str, depth: int = 100) -> dict:
        return self._get("/api/v3/depth", {"symbol": symbol, "limit": depth})
