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
from src.binance_rest import ClientBinance
from src.preprocessing import COLONNES_KLINE, normaliser_kline_websocket, normaliser_klines


async def capturer_websocket(symbole: str, timeout: float = 20) -> dict:
    url = f"{config.BASE_WS}/ws/{symbole.lower()}@kline_1m"
    async with websockets.connect(url) as ws:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


def main():
    config.SAMPLES.mkdir(parents=True, exist_ok=True)
    client = ClientBinance()
    symbole = "BTCUSDT"

    infos = client.infos_marches(config.PAIRES)
    brut_rest = client.klines(symbole, "1h", limite=3)
    ticker = client.ticker_24h(config.PAIRES)
    carnet = client.carnet_ordres(symbole, profondeur=5)
    brut_ws = asyncio.run(capturer_websocket(symbole))

    df = normaliser_klines(brut_rest, symbole, "1h")
    normalisee = json.loads(df.head(1).to_json(orient="records", date_format="iso"))[0]
    ws_normalise = {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                    for k, v in normaliser_kline_websocket(brut_ws).items()}

    echantillon = {
        "_metadonnees": {
            "projet": "jul26_bde_crypto - Etape 1 : decouverte des sources de donnees",
            "genere_le": datetime.now(timezone.utc).isoformat(),
            "source": config.BASE_REST,
            "paires_du_projet": config.PAIRES,
            "intervalle": config.INTERVALLE,
        },
        "1_rest_klines_brut": {
            "endpoint": "GET /api/v3/klines",
            "parametres": {"symbol": symbole, "interval": "1h", "limit": 3},
            "poids_requete": config.POIDS_REQUETE_KLINES,
            "remarque": "Tableau POSITIONNEL sans noms de champs ; prix envoyes en chaines.",
            "ordre_des_champs": COLONNES_KLINE,
            "reponse": brut_rest,
        },
        "2_rest_klines_normalise": {
            "remarque": "Champs nommes, prix en float, horodatages en datetime UTC, champ 'ignore' supprime.",
            "resultat": normalisee,
        },
        "3_websocket_kline_brut": {
            "flux": f"{symbole.lower()}@kline_1m",
            "remarque": "Cles d'une lettre. 'x' indique si la bougie est cloturee : "
                        "seul x=true est definitif (~1 message sur 15).",
            "bougie_cloturee": brut_ws["k"]["x"],
            "message": brut_ws,
        },
        "4_websocket_normalise": {
            "remarque": "Schema IDENTIQUE au REST normalise : les deux sources convergent.",
            "resultat": ws_normalise,
        },
        "5_exchange_info": {
            "endpoint": "GET /api/v3/exchangeInfo",
            "remarque": "Metadonnees de reference : alimenteront la table 'symboles' a l'etape 2.",
            "limites_api": infos["rateLimits"],
            "symboles": [
                {
                    "symbol": s["symbol"], "baseAsset": s["baseAsset"],
                    "quoteAsset": s["quoteAsset"], "status": s["status"],
                    "filtres_cles": {f["filterType"]: f for f in s["filters"]
                                     if f["filterType"] in ("PRICE_FILTER", "LOT_SIZE")},
                }
                for s in infos["symbols"]
            ],
        },
        "6_ticker_24h": {
            "endpoint": "GET /api/v3/ticker/24hr",
            "remarque": "Statistiques glissantes, utilisees ici pour comparer la liquidite des paires.",
            "reponse": ticker,
        },
        "7_carnet_ordres": {
            "endpoint": "GET /api/v3/depth",
            "remarque": "Profondeur de marche a l'instant t. Non conserve en historique "
                        "(volume trop important), documente pour justifier ce choix.",
            "reponse": carnet,
        },
    }

    sortie = config.SAMPLES / "exemple_donnees_binance.json"
    sortie.write_text(json.dumps(echantillon, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Ecrit : {sortie}  ({sortie.stat().st_size/1024:.1f} ko)")

    for cle in echantillon:
        if not cle.startswith("_"):
            print(f"  - {cle}")


if __name__ == "__main__":
    main()
