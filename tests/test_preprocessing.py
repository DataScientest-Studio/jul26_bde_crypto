"""Tests du pre-processing.

On teste ici les regles qui, si elles cassaient, corromprait silencieusement
tout l'historique sans lever d'erreur : conversion de type, fuseau horaire,
convergence REST/WebSocket, deduplication.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.preprocessing import (
    TARGET_SCHEMA,
    check_quality,
    deduplicate,
    interval_to_timedelta,
    normalize_klines,
    normalize_ws_kline,
)

# Reponse REST reelle (BTCUSDT 1h, capturee le 2026-08-28)
REST_KLINES = [
    [1787918400000, "79604.12000000", "79727.36000000", "79306.11000000",
     "79421.05000000", "400.05317000", 1787921999999, "31801379.67835110",
     113842, "172.31828000", "13699285.03758190", "0"],
    [1787922000000, "79421.04000000", "79635.01000000", "79034.56000000",
     "79330.75000000", "843.22470000", 1787925599999, "66877783.00563640",
     217173, "461.17816000", "36584174.98766420", "0"],
]

# Message WebSocket reel (@kline_1m)
WS_MESSAGE = {
    "e": "kline", "E": 1787927998015, "s": "BTCUSDT",
    "k": {"t": 1787927940000, "T": 1787927999999, "s": "BTCUSDT", "i": "1m",
          "o": "79616.16000000", "c": "79459.54000000", "h": "79658.00000000",
          "l": "79458.00000000", "v": "37.35743000", "n": 9855, "x": True,
          "q": "2973091.53580100", "V": "18.36197000", "Q": "1461489.57256680"},
}


def test_schema_is_respected():
    df = normalize_klines(REST_KLINES, "BTCUSDT", "1h")
    assert list(df.columns) == TARGET_SCHEMA
    assert len(df) == 2


def test_prices_converted_to_numeric():
    """Le bug le plus insidieux : des prix restes en chaine passent les
    controles visuels mais faussent toute comparaison ("9000" > "79600")."""
    df = normalize_klines(REST_KLINES, "BTCUSDT", "1h")
    for column in ("open", "high", "low", "close", "volume"):
        assert pd.api.types.is_float_dtype(df[column]), f"{column} n'est pas numerique"
    assert df["close"].iloc[0] == pytest.approx(79421.05)
    assert df["nb_trades"].iloc[0] == 113842


def test_timestamps_are_explicit_utc():
    """Sans fuseau, pandas suppose l'heure locale et decale tout l'historique."""
    df = normalize_klines(REST_KLINES, "BTCUSDT", "1h")
    assert str(df["open_time"].dt.tz) == "UTC"
    assert df["open_time"].iloc[0] == pd.Timestamp("2026-08-28 12:00:00", tz="UTC")


def test_ignore_column_is_dropped():
    df = normalize_klines(REST_KLINES, "BTCUSDT", "1h")
    assert "ignore" not in df.columns


def test_symbol_and_interval_are_added():
    """La reponse de Binance ne contient ni la paire ni l'intervalle : sans ces
    colonnes, impossible d'empiler plusieurs paires dans une meme table."""
    df = normalize_klines(REST_KLINES, "ETHUSDT", "4h")
    assert (df["symbol"] == "ETHUSDT").all()
    assert (df["interval"] == "4h").all()


def test_ohlc_consistency_on_real_data():
    df = normalize_klines(REST_KLINES, "BTCUSDT", "1h")
    assert (df["high"] >= df[["open", "close", "low"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close", "high"]].min(axis=1)).all()


def test_rest_and_websocket_converge():
    """Regle d'architecture centrale : les deux sources doivent produire
    exactement les memes champs, sinon l'etape 2 gererait deux schemas."""
    rest_fields = set(normalize_klines(REST_KLINES, "BTCUSDT", "1h").columns)
    ws_fields = set(normalize_ws_kline(WS_MESSAGE).keys())
    assert rest_fields == ws_fields


def test_websocket_types_are_correct():
    candle = normalize_ws_kline(WS_MESSAGE)
    assert isinstance(candle["close"], float)
    assert candle["close"] == pytest.approx(79459.54)
    assert isinstance(candle["nb_trades"], int)
    assert str(candle["open_time"].tz) == "UTC"


def test_deduplicate_keeps_latest():
    """Une bougie encore ouverte est re-emise : la version la plus recente
    est la plus complete."""
    df = normalize_klines(REST_KLINES + [REST_KLINES[0]], "BTCUSDT", "1h")
    assert len(df) == 3
    assert len(deduplicate(df)) == 2


def test_deduplicate_keeps_distinct_intervals():
    """Meme paire, meme heure, mais deux intervalles : ce sont deux bougies
    differentes. La cle de deduplication doit inclure l'intervalle."""
    hourly = normalize_klines(REST_KLINES, "BTCUSDT", "1h")
    four_hourly = normalize_klines(REST_KLINES, "BTCUSDT", "4h")
    combined = pd.concat([hourly, four_hourly], ignore_index=True)
    assert len(deduplicate(combined)) == 4


def test_quality_check_counts_missing_candles():
    complete = normalize_klines(REST_KLINES, "BTCUSDT", "1h")
    assert check_quality(complete, "1h")["missing_candles"] == 0
    assert check_quality(complete, "1h")["completeness_pct"] == 100.0


def test_quality_check_detects_a_gap():
    """On retire une bougie du milieu : le controle doit voir le trou."""
    raw = REST_KLINES + [
        [1787925600000, "79330.75000000", "79400.00000000", "79300.00000000",
         "79350.00000000", "100.00000000", 1787929199999, "7935000.00000000",
         5000, "50.00000000", "3967500.00000000", "0"],
    ]
    df = normalize_klines(raw, "BTCUSDT", "1h").drop(index=1).reset_index(drop=True)
    result = check_quality(df, "1h")
    assert result["missing_candles"] == 1
    assert result["gap_count"] == 1


def test_empty_input_does_not_raise():
    df = normalize_klines([], "BTCUSDT", "1h")
    assert df.empty
    assert list(df.columns) == TARGET_SCHEMA
    assert check_quality(df, "1h") == {"status": "EMPTY"}


@pytest.mark.parametrize(
    "interval,expected_minutes",
    [("1m", 1), ("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240), ("1d", 1440), ("1w", 10080)],
)
def test_all_profile_intervals_are_supported(interval, expected_minutes):
    """Chaque intervalle utilise par un profil doit etre convertible en duree,
    sinon le controle qualite planterait au moment de la collecte."""
    assert interval_to_timedelta(interval).total_seconds() / 60 == expected_minutes


def test_unknown_interval_raises():
    with pytest.raises(ValueError, match="non gere"):
        interval_to_timedelta("42s")
