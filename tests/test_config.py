"""Tests de la configuration des profils de trading.

La resolution des intervalles est la seule logique non triviale de config.py :
quand deux profils demandent le meme intervalle avec des profondeurs
d'historique differentes, il faut retenir la plus longue.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.binance_rest import start_date_for
from src.preprocessing import INTERVAL_SECONDS


def test_every_profile_interval_is_supported():
    """Un profil ne doit jamais reclamer un intervalle que le pre-processing
    ne sait pas mesurer : la collecte echouerait au controle qualite."""
    for name, profile in config.TRADING_PROFILES.items():
        for interval in profile["intervals"]:
            assert interval in INTERVAL_SECONDS, f"{name} demande {interval}, non gere"


def test_default_profile_exists():
    assert config.DEFAULT_PROFILE in config.TRADING_PROFILES


def test_single_profile_resolution():
    resolved = config.resolve_intervals(["scalping"])
    assert set(resolved) == {"1m", "5m", "15m"}
    assert all(depth == 180 for depth in resolved.values())


def test_longest_history_wins_on_overlap():
    """15m appartient a scalping (180 j) et day_trading (730 j).
    Collecter large satisfait les deux ; l'inverse priverait day_trading."""
    resolved = config.resolve_intervals(["scalping", "day_trading"])
    assert resolved["15m"] == 730
    assert resolved["1m"] == 180


def test_full_history_beats_any_number_of_days():
    """swing demande 4h sur tout l'historique (None), day_trading sur 730 j.
    None doit l'emporter : c'est la profondeur maximale."""
    resolved = config.resolve_intervals(["day_trading", "swing"])
    assert resolved["4h"] is None
    # L'ordre des profils ne doit rien changer au resultat.
    assert config.resolve_intervals(["swing", "day_trading"])["4h"] is None


def test_all_profiles_together():
    resolved = config.resolve_intervals(list(config.TRADING_PROFILES))
    assert set(resolved) == {"1m", "5m", "15m", "1h", "4h", "1d", "1w"}


def test_unknown_profile_raises():
    with pytest.raises(ValueError, match="Profil inconnu"):
        config.resolve_intervals(["daytrading"])


def test_start_date_never_precedes_project_floor():
    """Remonter 5 ans en arriere depasserait la date de creation de SOLUSDT :
    on doit retomber sur le plancher du projet."""
    assert start_date_for(None) == config.HISTORY_START
    assert start_date_for(365 * 20) == config.HISTORY_START
    assert start_date_for(30) > config.HISTORY_START
