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
COLONNES_KLINE = [
    "open_time",             # 0  debut de bougie (ms UTC)
    "open", "high", "low", "close",   # 1-4  prix OHLC (chaines !)
    "volume",                # 5  volume en actif de base (ex: BTC)
    "close_time",            # 6  fin de bougie (ms UTC)
    "quote_volume",          # 7  volume en actif de cotation (ex: USDT)
    "nb_trades",             # 8  nombre de transactions
    "taker_buy_base",        # 9  volume achete a l'initiative de l'acheteur
    "taker_buy_quote",       # 10 idem, en actif de cotation
    "ignore",                # 11 champ non documente, toujours "0"
]

COLONNES_NUMERIQUES = [
    "open", "high", "low", "close", "volume",
    "quote_volume", "taker_buy_base", "taker_buy_quote",
]

# Schema cible commun REST / WebSocket
SCHEMA_FINAL = [
    "symbol", "interval", "open_time", "close_time",
    "open", "high", "low", "close",
    "volume", "quote_volume", "nb_trades",
    "taker_buy_base", "taker_buy_quote",
]


def normaliser_klines(brut: list[list], symbole: str, intervalle: str) -> pd.DataFrame:
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
    if not brut:
        return pd.DataFrame(columns=SCHEMA_FINAL)

    df = pd.DataFrame(brut, columns=COLONNES_KLINE)
    df = df.drop(columns=["ignore"])  # champ constant, aucune information

    for col in COLONNES_NUMERIQUES:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["nb_trades"] = pd.to_numeric(df["nb_trades"], errors="coerce").astype("int64")

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)

    df.insert(0, "symbol", symbole)
    df.insert(1, "interval", intervalle)

    return df[SCHEMA_FINAL]


def normaliser_kline_websocket(message: dict) -> dict:
    """Transforme un message WebSocket `@kline` vers le MEME schema que le REST.

    Le flux temps reel emet ~15 mises a jour par minute pour une bougie 1m :
    seule la derniere, marquee `x: true`, est definitive. Les autres sont des
    etats intermediaires. Le filtrage se fait en amont (voir binance_ws.py) ;
    ici on ne fait que traduire les cles d'une lettre en noms explicites.
    """
    k = message["k"]
    return {
        "symbol": k["s"],
        "interval": k["i"],
        "open_time": pd.to_datetime(k["t"], unit="ms", utc=True),
        "close_time": pd.to_datetime(k["T"], unit="ms", utc=True),
        "open": float(k["o"]),
        "high": float(k["h"]),
        "low": float(k["l"]),
        "close": float(k["c"]),
        "volume": float(k["v"]),
        "quote_volume": float(k["q"]),
        "nb_trades": int(k["n"]),
        "taker_buy_base": float(k["V"]),
        "taker_buy_quote": float(k["Q"]),
    }


def controler_qualite(df: pd.DataFrame, intervalle: str) -> dict:
    """Audit du jeu de donnees. Ne corrige rien : constate et chiffre.

    Separer le controle de la correction est volontaire. Un trou dans les
    donnees peut venir d'une panne de collecte (a corriger) ou d'un arret de
    cotation reel sur Binance (a conserver tel quel). Seul un humain tranche.
    """
    if df.empty:
        return {"statut": "VIDE"}

    pas = pd.Timedelta(intervalle.replace("m", "min") if intervalle.endswith("m") else intervalle)
    attendu = int((df["open_time"].max() - df["open_time"].min()) / pas) + 1

    ecarts = df["open_time"].diff().dropna()
    trous = ecarts[ecarts > pas]

    # Coherence OHLC : le plus haut doit dominer, le plus bas doit etre domine.
    incoherences = df[
        (df["high"] < df[["open", "close", "low"]].max(axis=1))
        | (df["low"] > df[["open", "close", "high"]].min(axis=1))
    ]

    return {
        "symbole": df["symbol"].iloc[0],
        "lignes": len(df),
        "periode_debut": str(df["open_time"].min()),
        "periode_fin": str(df["open_time"].max()),
        "bougies_attendues": attendu,
        "bougies_manquantes": attendu - len(df),
        "taux_completude_pct": round(100 * len(df) / attendu, 4),
        "doublons_open_time": int(df["open_time"].duplicated().sum()),
        "valeurs_nulles": int(df[SCHEMA_FINAL].isna().sum().sum()),
        "nb_trous": int(len(trous)),
        "plus_grand_trou": str(trous.max()) if len(trous) else None,
        "incoherences_ohlc": int(len(incoherences)),
        "volume_nul": int((df["volume"] == 0).sum()),
    }


def dedupliquer(df: pd.DataFrame) -> pd.DataFrame:
    """Deduplication sur la cle metier (symbol, open_time).

    Necessaire car la pagination REST peut renvoyer une bougie a cheval sur
    deux lots, et car un redemarrage de collecte rejoue une partie du passe.
    On garde la DERNIERE occurrence : sur une bougie encore ouverte, la valeur
    la plus recente est la plus complete.
    """
    avant = len(df)
    df = df.sort_values("open_time").drop_duplicates(subset=["symbol", "open_time"], keep="last")
    if avant != len(df):
        logger.info("Deduplication : %d lignes supprimees", avant - len(df))
    return df.reset_index(drop=True)
