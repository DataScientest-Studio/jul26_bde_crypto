"""Collecteur WebSocket Binance - donnees de marche en temps reel.

Le REST donne le passe, le WebSocket donne le present. Les deux alimentent le
meme schema (voir preprocessing.py), ce qui permettra a l'etape 2 d'ecrire
dans les memes tables sans distinction de provenance.

Contraintes de l'API relevees en mesure directe le 2026-08-28 :
  - `@bookTicker` sur BTCUSDT : ~420 msg/s
  - `@trade`      sur BTCUSDT : ~136 msg/s
  - `@kline_1m`               : ~0,4 msg/s (soit ~15 messages par bougie)
  Un seul de ces 15 messages porte `x: true` : la bougie cloturee, definitive.
  Ecrire les 14 autres en base reviendrait a stocker ~96 % de doublons.

  - Binance ferme toute connexion au bout de 24 h : la reconnexion n'est pas
    une precaution, c'est une certitude a gerer.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

import websockets

from . import config

logger = logging.getLogger(__name__)


def build_stream_url(symbols: list[str], stream: str = "kline_1m") -> str:
    """Assemble une URL de flux combine.

    Un flux combine multiplexe N paires sur UNE connexion. A l'inverse, ouvrir
    une connexion par paire consomme un quota de connexions et multiplie les
    points de panne pour un benefice nul.
    """
    parts = [f"{symbol.lower()}@{stream}" for symbol in symbols]
    return f"{config.WS_BASE_URL}/stream?streams=" + "/".join(parts)


class WebSocketCollector:
    """Ecoute un flux combine et delegue chaque bougie CLOTUREE a un callback.

    Le callback recoit un dict deja normalise au schema commun : c'est le point
    d'accroche pour l'etape 2 (ecriture en base) sans toucher a ce fichier.
    """

    def __init__(
        self,
        symbols: list[str],
        stream: str = "kline_1m",
        on_candle: Callable[[dict], None] | None = None,
    ):
        self.symbols = symbols
        self.stream = stream
        self.on_candle = on_candle or (lambda candle: None)
        self.url = build_stream_url(symbols, stream)
        self.stats = {"received": 0, "closed": 0, "skipped": 0, "reconnections": 0}

    async def listen(self, max_duration: float | None = None):
        """Boucle d'ecoute avec reconnexion automatique et backoff exponentiel."""
        from .preprocessing import normalize_ws_kline

        delay = 1
        started_at = asyncio.get_event_loop().time()

        while True:
            try:
                async with websockets.connect(
                    self.url, ping_interval=20, ping_timeout=60
                ) as socket:
                    logger.info(
                        "Connecte : %d paires, flux %s", len(self.symbols), self.stream
                    )
                    delay = 1  # connexion reussie : on remet le backoff a zero

                    async for raw in socket:
                        message = json.loads(raw)
                        payload = message.get("data", message)
                        self.stats["received"] += 1

                        # Filtre central : on ne retient que les bougies figees.
                        if payload.get("e") == "kline" and payload["k"]["x"]:
                            self.stats["closed"] += 1
                            self.on_candle(normalize_ws_kline(payload))
                        else:
                            self.stats["skipped"] += 1

                        if (
                            max_duration
                            and asyncio.get_event_loop().time() - started_at > max_duration
                        ):
                            logger.info("Duree max atteinte. Statistiques : %s", self.stats)
                            return

            except (websockets.ConnectionClosed, OSError) as exc:
                if (
                    max_duration
                    and asyncio.get_event_loop().time() - started_at > max_duration
                ):
                    return
                self.stats["reconnections"] += 1
                logger.warning(
                    "Connexion perdue (%s). Reconnexion dans %ss.",
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
                # Backoff exponentiel plafonne : ne pas marteler un service en panne.
                delay = min(delay * 2, 60)
