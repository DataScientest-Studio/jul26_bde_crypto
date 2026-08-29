"""Collecteur WebSocket Binance - donnees de marche en temps reel.

Le REST donne le passe, le WebSocket donne le present. Les deux alimentent le
meme schema (voir preprocessing.py), ce qui permettra a l'etape 2 d'ecrire
dans les memes tables sans distinction de provenance.

Contraintes de l'API relevees en mesure directe le 2026-08-28 :
  - `@bookTicker` sur BTCUSDT : ~420 msg/s
  - `@trade`      sur BTCUSDT : ~136 msg/s
  - `@kline_1m`               : ~0,4 msg/s (soit ~15 messages par bougie)
  Un seul de ces 15 messages porte `x: true` : la bougie cloturee, definitive.
  Ecrire les 14 autres en base reviendrait a stocker ~93 % de doublons.

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


def construire_url(symboles: list[str], flux: str = "kline_1m") -> str:
    """Assemble une URL de flux combine.

    Un flux combine multiplexe N paires sur UNE connexion. A l'inverse, ouvrir
    une connexion par paire consomme un quota de connexions et multiplie les
    points de panne pour un benefice nul.
    """
    parties = [f"{s.lower()}@{flux}" for s in symboles]
    return f"{config.BASE_WS}/stream?streams=" + "/".join(parties)


class CollecteurWebSocket:
    """Ecoute un flux combine et delegue chaque bougie CLOTUREE a un callback.

    Le callback recoit un dict deja normalise au schema commun : c'est le point
    d'accroche pour l'etape 2 (ecriture en base) sans toucher a ce fichier.
    """

    def __init__(self, symboles: list[str], flux: str = "kline_1m",
                 sur_bougie: Callable[[dict], None] | None = None):
        self.symboles = symboles
        self.flux = flux
        self.sur_bougie = sur_bougie or (lambda b: None)
        self.url = construire_url(symboles, flux)
        self.stats = {"recus": 0, "cloturees": 0, "ignores": 0, "reconnexions": 0}

    async def ecouter(self, duree_max: float | None = None):
        """Boucle d'ecoute avec reconnexion automatique et backoff exponentiel."""
        from .preprocessing import normaliser_kline_websocket

        attente = 1
        debut = asyncio.get_event_loop().time()

        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=60) as ws:
                    logger.info("Connecte : %d paires, flux %s", len(self.symboles), self.flux)
                    attente = 1  # connexion reussie : on remet le backoff a zero

                    async for brut in ws:
                        message = json.loads(brut)
                        donnees = message.get("data", message)
                        self.stats["recus"] += 1

                        # Filtre central : on ne retient que les bougies figees.
                        if donnees.get("e") == "kline" and donnees["k"]["x"]:
                            self.stats["cloturees"] += 1
                            self.sur_bougie(normaliser_kline_websocket(donnees))
                        else:
                            self.stats["ignores"] += 1

                        if duree_max and asyncio.get_event_loop().time() - debut > duree_max:
                            logger.info("Duree max atteinte. Statistiques : %s", self.stats)
                            return

            except (websockets.ConnectionClosed, OSError) as e:
                if duree_max and asyncio.get_event_loop().time() - debut > duree_max:
                    return
                self.stats["reconnexions"] += 1
                logger.warning("Connexion perdue (%s). Reconnexion dans %ss.", type(e).__name__, attente)
                await asyncio.sleep(attente)
                # Backoff exponentiel plafonne : ne pas marteler un service en panne.
                attente = min(attente * 2, 60)
