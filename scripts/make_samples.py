"""Genere les fichiers d'exemple exiges par le livrable de l'etape 1.

Chaque exemple montre le MEME objet metier a trois stades : brut REST, brut
WebSocket, normalise. C'est la demonstration visuelle du role du pre-processing.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets

from src import config
from src.binance_rest import BinanceClient
from src.preprocessing import KLINE_COLUMNS, normalize_klines, normalize_ws_kline


async def capture_ws_message(symbol: str, timeout: float = 20) -> dict:
    """Recupere un seul message du flux temps reel, pour l'exemple."""
    url = f"{config.WS_BASE_URL}/ws/{symbol.lower()}@kline_1m"
    async with websockets.connect(url) as socket:
        return json.loads(await asyncio.wait_for(socket.recv(), timeout=timeout))


def main():
    config.SAMPLES.mkdir(parents=True, exist_ok=True)
    client = BinanceClient()
    symbol = "BTCUSDT"

    info = client.exchange_info(config.PAIRS)
    raw_rest = client.klines(symbol, "1h", limit=3)
    ticker = client.ticker_24h(config.PAIRS)
    book = client.order_book(symbol, depth=5)
    raw_ws = asyncio.run(capture_ws_message(symbol))

    df = normalize_klines(raw_rest, symbol, "1h")
    normalized = json.loads(df.head(1).to_json(orient="records", date_format="iso"))[0]
    ws_normalized = {
        key: (value.isoformat() if hasattr(value, "isoformat") else value)
        for key, value in normalize_ws_kline(raw_ws).items()
    }

    sample = {
        "_metadata": {
            "projet": "jul26_bde_crypto - Etape 1 : decouverte des sources de donnees",
            "genere_le": datetime.now(timezone.utc).isoformat(),
            "source": config.REST_BASE_URL,
            "paires_du_projet": config.PAIRS,
            "profils_de_trading": {
                name: {
                    "intervalles": profile["intervals"],
                    "profondeur_jours": profile["history_days"],
                }
                for name, profile in config.TRADING_PROFILES.items()
            },
        },
        "1_rest_klines_brut": {
            "endpoint": "GET /api/v3/klines",
            "parametres": {"symbol": symbol, "interval": "1h", "limit": 3},
            "poids_requete": config.KLINES_REQUEST_WEIGHT,
            "remarque": "Tableau POSITIONNEL sans noms de champs ; prix envoyes en chaines.",
            "ordre_des_champs": KLINE_COLUMNS,
            "reponse": raw_rest,
        },
        "2_rest_klines_normalise": {
            "remarque": "Champs nommes, prix en float, horodatages en datetime UTC, "
                        "champ 'ignore' supprime, symbole et intervalle ajoutes.",
            "resultat": normalized,
        },
        "3_websocket_kline_brut": {
            "flux": f"{symbol.lower()}@kline_1m",
            "remarque": "Cles d'une lettre. 'x' indique si la bougie est cloturee : "
                        "seul x=true est definitif (~1 message sur 15).",
            "bougie_cloturee": raw_ws["k"]["x"],
            "message": raw_ws,
        },
        "4_websocket_normalise": {
            "remarque": "Schema IDENTIQUE au REST normalise : les deux sources convergent.",
            "resultat": ws_normalized,
        },
        "5_exchange_info": {
            "endpoint": "GET /api/v3/exchangeInfo",
            "remarque": "Metadonnees de reference : alimenteront la table 'symbols' a l'etape 2.",
            "limites_api": info["rateLimits"],
            "symboles": [
                {
                    "symbol": s["symbol"],
                    "baseAsset": s["baseAsset"],
                    "quoteAsset": s["quoteAsset"],
                    "status": s["status"],
                    "filtres_cles": {
                        f["filterType"]: f
                        for f in s["filters"]
                        if f["filterType"] in ("PRICE_FILTER", "LOT_SIZE")
                    },
                }
                for s in info["symbols"]
            ],
        },
        "6_ticker_24h": {
            "endpoint": "GET /api/v3/ticker/24hr",
            "remarque": "Statistiques glissantes, utilisees ici pour comparer la liquidite.",
            "reponse": ticker,
        },
        "7_carnet_ordres": {
            "endpoint": "GET /api/v3/depth",
            "remarque": "Profondeur de marche a l'instant t. Non conserve en historique "
                        "(aucun historique disponible cote Binance), documente pour "
                        "justifier ce choix.",
            "reponse": book,
        },
    }

    output = config.SAMPLES / "exemple_donnees_binance.json"
    output.write_text(json.dumps(sample, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Ecrit : {output}  ({output.stat().st_size / 1024:.1f} ko)")

    for key in sample:
        if not key.startswith("_"):
            print(f"  - {key}")


if __name__ == "__main__":
    main()
