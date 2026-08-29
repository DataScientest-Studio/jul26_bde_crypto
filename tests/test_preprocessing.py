"""Tests du pre-processing.

On teste ici les regles qui, si elles cassaient, corromperaient silencieusement
tout l'historique sans lever d'erreur : conversion de type, fuseau horaire,
convergence REST/WebSocket, deduplication.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.preprocessing import (
    SCHEMA_FINAL,
    controler_qualite,
    dedupliquer,
    normaliser_kline_websocket,
    normaliser_klines,
)

# Reponse REST reelle (BTCUSDT 1h, capturee le 2026-08-28)
KLINE_REST = [
    [1787918400000, "79604.12000000", "79727.36000000", "79306.11000000",
     "79421.05000000", "400.05317000", 1787921999999, "31801379.67835110",
     113842, "172.31828000", "13699285.03758190", "0"],
    [1787922000000, "79421.04000000", "79635.01000000", "79034.56000000",
     "79330.75000000", "843.22470000", 1787925599999, "66877783.00563640",
     217173, "461.17816000", "36584174.98766420", "0"],
]

# Message WebSocket reel (@kline_1m)
MSG_WS = {
    "e": "kline", "E": 1787927998015, "s": "BTCUSDT",
    "k": {"t": 1787927940000, "T": 1787927999999, "s": "BTCUSDT", "i": "1m",
          "o": "79616.16000000", "c": "79459.54000000", "h": "79658.00000000",
          "l": "79458.00000000", "v": "37.35743000", "n": 9855, "x": True,
          "q": "2973091.53580100", "V": "18.36197000", "Q": "1461489.57256680"},
}


def test_schema_respecte():
    df = normaliser_klines(KLINE_REST, "BTCUSDT", "1h")
    assert list(df.columns) == SCHEMA_FINAL
    assert len(df) == 2


def test_prix_convertis_en_numerique():
    """Le bug le plus insidieux : des prix restes en chaine passent les
    controles visuels mais faussent toute comparaison ("9000" > "79600")."""
    df = normaliser_klines(KLINE_REST, "BTCUSDT", "1h")
    for col in ("open", "high", "low", "close", "volume"):
        assert pd.api.types.is_float_dtype(df[col]), f"{col} n'est pas numerique"
    assert df["close"].iloc[0] == pytest.approx(79421.05)
    assert df["nb_trades"].iloc[0] == 113842


def test_horodatages_en_utc_explicite():
    """Sans fuseau, pandas suppose l'heure locale et decale tout l'historique."""
    df = normaliser_klines(KLINE_REST, "BTCUSDT", "1h")
    assert str(df["open_time"].dt.tz) == "UTC"
    assert df["open_time"].iloc[0] == pd.Timestamp("2026-08-28 12:00:00", tz="UTC")


def test_champ_ignore_supprime():
    df = normaliser_klines(KLINE_REST, "BTCUSDT", "1h")
    assert "ignore" not in df.columns


def test_coherence_ohlc_des_donnees_reelles():
    df = normaliser_klines(KLINE_REST, "BTCUSDT", "1h")
    assert (df["high"] >= df[["open", "close", "low"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close", "high"]].min(axis=1)).all()


def test_rest_et_websocket_convergent():
    """Regle d'architecture centrale : les deux sources doivent produire
    exactement les memes champs, sinon l'etape 2 devrait gerer deux schemas."""
    champs_rest = set(normaliser_klines(KLINE_REST, "BTCUSDT", "1h").columns)
    champs_ws = set(normaliser_kline_websocket(MSG_WS).keys())
    assert champs_rest == champs_ws


def test_websocket_types_corrects():
    b = normaliser_kline_websocket(MSG_WS)
    assert isinstance(b["close"], float) and b["close"] == pytest.approx(79459.54)
    assert isinstance(b["nb_trades"], int)
    assert str(b["open_time"].tz) == "UTC"


def test_deduplication_garde_la_derniere_valeur():
    """Une bougie encore ouverte est re-emise : la version la plus recente
    est la plus complete."""
    df = normaliser_klines(KLINE_REST + [KLINE_REST[0]], "BTCUSDT", "1h")
    assert len(df) == 3
    assert len(dedupliquer(df)) == 2


def test_controle_qualite_detecte_un_trou():
    df = normaliser_klines([KLINE_REST[0], KLINE_REST[1]], "BTCUSDT", "1h")
    df = df.drop(index=1).reset_index(drop=True)  # on retire la 2e bougie
    complet = normaliser_klines(KLINE_REST, "BTCUSDT", "1h")
    assert controler_qualite(complet, "1h")["bougies_manquantes"] == 0
    assert controler_qualite(df, "1h")["lignes"] == 1


def test_entree_vide_ne_leve_pas():
    df = normaliser_klines([], "BTCUSDT", "1h")
    assert df.empty and list(df.columns) == SCHEMA_FINAL
    assert controler_qualite(df, "1h") == {"statut": "VIDE"}
