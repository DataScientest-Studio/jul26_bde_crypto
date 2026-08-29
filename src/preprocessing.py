"""Normalisation et controle qualite des donnees de marche.

Objectif central : REST et WebSocket arrivent dans deux formats totalement
differents (tableau positionnel vs objet a cles d'une lettre) mais decrivent
le MEME objet metier, une bougie. Ce module les fait converger vers un schema
unique. Tout ce qui est en aval (base de donnees etape 2, modele etape 3) ne
voit donc qu'un seul format et ignore d'ou vient la donnee.
"""
from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)

# L'API REST renvoie un tableau de 12 elements SANS noms. L'ordre est le
# contrat d'interface : le documenter ici evite les indices magiques (k[4]).
KLINE_COLUMNS = [
    "open_time",                     # 0  debut de bougie (ms UTC)
    "open", "high", "low", "close",  # 1-4  prix OHLC (chaines !)
    "volume",                        # 5  volume en actif de base (ex: BTC)
    "close_time",                    # 6  fin de bougie (ms UTC)
    "quote_volume",                  # 7  volume en actif de cotation (ex: USDT)
    "nb_trades",                     # 8  nombre de transactions
    "taker_buy_base",                # 9  volume achete a l'initiative de l'acheteur
    "taker_buy_quote",               # 10 idem, en actif de cotation
    "ignore",                        # 11 champ non documente, toujours "0"
]

NUMERIC_COLUMNS = [
    "open", "high", "low", "close", "volume",
    "quote_volume", "taker_buy_base", "taker_buy_quote",
]

# Schema cible commun REST / WebSocket
TARGET_SCHEMA = [
    "symbol", "interval", "open_time", "close_time",
    "open", "high", "low", "close",
    "volume", "quote_volume", "nb_trades",
    "taker_buy_base", "taker_buy_quote",
]

# Duree d'une bougie, par intervalle Binance. Sert au controle de completude :
# sans elle, impossible de savoir combien de bougies on DEVRAIT avoir.
INTERVAL_DURATIONS = {
    "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h", "12h": "12h",
    "1d": "1D", "3d": "3D", "1w": "7D",
}


def interval_to_timedelta(interval: str) -> pd.Timedelta:
    """Traduit un intervalle Binance ("15m", "1w") en duree pandas."""
    if interval not in INTERVAL_DURATIONS:
        raise ValueError(f"Intervalle non gere : {interval!r}")
    return pd.Timedelta(INTERVAL_DURATIONS[interval])


def normalize_klines(raw: list[list], symbol: str, interval: str) -> pd.DataFrame:
    """Transforme la reponse brute de /api/v3/klines en DataFrame exploitable.

    Trois corrections indispensables :
      1. Nommer les colonnes (le brut est positionnel).
      2. Convertir les prix : Binance les envoie en CHAINES de caracteres,
         volontairement, pour ne pas perdre de precision en JSON. Les laisser
         ainsi ferait echouer tout calcul ("79600.87" > "9000" est faux en
         comparaison lexicographique).
      3. Convertir les timestamps ms en datetime UTC explicite. Sans fuseau,
         pandas supposerait l'heure locale et decalerait tout l'historique.
    """
    if not raw:
        return pd.DataFrame(columns=TARGET_SCHEMA)

    df = pd.DataFrame(raw, columns=KLINE_COLUMNS)
    df = df.drop(columns=["ignore"])  # champ constant, aucune information

    for column in NUMERIC_COLUMNS:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["nb_trades"] = pd.to_numeric(df["nb_trades"], errors="coerce").astype("int64")

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)

    # La reponse de Binance ne contient NI la paire NI l'intervalle : l'API
    # suppose qu'on se souvient de sa propre question. Sans ces deux colonnes,
    # impossible d'empiler plusieurs paires dans une meme table a l'etape 2.
    df.insert(0, "symbol", symbol)
    df.insert(1, "interval", interval)

    return df[TARGET_SCHEMA]


def normalize_ws_kline(message: dict) -> dict:
    """Transforme un message WebSocket `@kline` vers le MEME schema que le REST.

    Le flux temps reel emet ~15 mises a jour par minute pour une bougie 1m :
    seule la derniere, marquee `x: true`, est definitive. Les autres sont des
    etats intermediaires. Le filtrage se fait en amont (voir binance_ws.py) ;
    ici on ne fait que traduire les cles d'une lettre en noms explicites.
    """
    kline = message["k"]
    return {
        "symbol": kline["s"],
        "interval": kline["i"],
        "open_time": pd.to_datetime(kline["t"], unit="ms", utc=True),
        "close_time": pd.to_datetime(kline["T"], unit="ms", utc=True),
        "open": float(kline["o"]),
        "high": float(kline["h"]),
        "low": float(kline["l"]),
        "close": float(kline["c"]),
        "volume": float(kline["v"]),
        "quote_volume": float(kline["q"]),
        "nb_trades": int(kline["n"]),
        "taker_buy_base": float(kline["V"]),
        "taker_buy_quote": float(kline["Q"]),
    }


def check_quality(df: pd.DataFrame, interval: str) -> dict:
    """Audit du jeu de donnees. Ne corrige rien : constate et chiffre.

    Separer le controle de la correction est volontaire. Un trou dans les
    donnees peut venir d'une panne de collecte (a corriger) ou d'un arret de
    cotation reel sur Binance (a conserver tel quel). Seul un humain tranche.
    """
    if df.empty:
        return {"status": "EMPTY"}

    step = interval_to_timedelta(interval)
    expected = int((df["open_time"].max() - df["open_time"].min()) / step) + 1

    deltas = df["open_time"].diff().dropna()
    gaps = deltas[deltas > step]

    # Coherence OHLC : le plus haut doit dominer, le plus bas doit etre domine.
    inconsistent = df[
        (df["high"] < df[["open", "close", "low"]].max(axis=1))
        | (df["low"] > df[["open", "close", "high"]].min(axis=1))
    ]

    return {
        "symbol": df["symbol"].iloc[0],
        "interval": interval,
        "rows": len(df),
        "period_start": str(df["open_time"].min()),
        "period_end": str(df["open_time"].max()),
        "expected_candles": expected,
        "missing_candles": expected - len(df),
        "completeness_pct": round(100 * len(df) / expected, 4),
        "duplicate_open_time": int(df["open_time"].duplicated().sum()),
        "null_values": int(df[TARGET_SCHEMA].isna().sum().sum()),
        "gap_count": int(len(gaps)),
        "largest_gap": str(gaps.max()) if len(gaps) else None,
        "ohlc_inconsistencies": int(len(inconsistent)),
        "zero_volume_candles": int((df["volume"] == 0).sum()),
    }


def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    """Deduplication sur la cle metier (symbol, interval, open_time).

    Necessaire car la pagination REST peut renvoyer une bougie a cheval sur
    deux lots, et car un redemarrage de collecte rejoue une partie du passe.
    On garde la DERNIERE occurrence : sur une bougie encore ouverte, la valeur
    la plus recente est la plus complete.
    """
    before = len(df)
    df = df.sort_values("open_time").drop_duplicates(
        subset=["symbol", "interval", "open_time"], keep="last"
    )
    if before != len(df):
        logger.info("Deduplication : %d lignes supprimees", before - len(df))
    return df.reset_index(drop=True)
