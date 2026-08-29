"""Configuration centrale du projet.

Tout ce qui est "parametre du projet" vit ici et nulle part ailleurs :
changer de paires ou d'intervalles ne doit jamais demander de toucher au
code de collecte. C'est ce qui rend la fonction de recuperation generique.
"""
from pathlib import Path

# --- Chemins ---------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = ROOT / "data" / "raw"
DATA_PROCESSED = ROOT / "data" / "processed"
SAMPLES = ROOT / "samples"
DOCS = ROOT / "docs"

# --- Perimetre fonctionnel -------------------------------------------------
# 5 paires majeures cotees en USDT. Choix justifie dans docs/rapport_etape1.md
PAIRS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]

# Debut d'historique le plus ancien possible : SOLUSDT n'existe que depuis
# 2020-08-11, c'est la paire contraignante. On aligne toutes les paires sur
# cette date pour disposer d'un axe temporel commun, indispensable pour toute
# feature cross-paires (etape 3).
HISTORY_START = "2020-09-01"

# --- Profils de trading ----------------------------------------------------
# Un bot de trading ne regarde pas le marche a la meme echelle selon la
# strategie visee. Chaque profil definit ses intervalles ET sa profondeur
# d'historique : un scalpeur n'a aucun usage de bougies d'une minute datant
# de 2020, alors qu'un swing trader a besoin de plusieurs cycles de marche.
#
# `history_days = None` signifie "tout l'historique disponible".
TRADING_PROFILES = {
    "scalping": {
        "label": "Scalping",
        "description": "Positions de quelques minutes. Cherche de tres petits "
                       "mouvements, tres frequents.",
        "intervals": ["1m", "5m", "15m"],
        "history_days": 180,
        "reason": "Au-dela de quelques mois, la microstructure du marche a trop "
                  "change pour rester pertinente a cette echelle.",
    },
    "day_trading": {
        "label": "Day trading",
        "description": "Positions ouvertes et fermees dans la journee.",
        "intervals": ["15m", "1h", "4h"],
        "history_days": 730,
        "reason": "Deux ans couvrent plusieurs regimes de marche (hausse, "
                  "baisse, stagnation) sans noyer le modele sous le bruit.",
    },
    "swing": {
        "label": "Swing trading",
        "description": "Positions tenues plusieurs jours a plusieurs semaines.",
        "intervals": ["4h", "1d", "1w"],
        "history_days": None,
        "reason": "Une bougie hebdomadaire ne produit que ~50 lignes par an : "
                  "il faut tout l'historique pour avoir de quoi entrainer.",
    },
}

# Profil utilise par defaut si aucun n'est precise en ligne de commande.
DEFAULT_PROFILE = "day_trading"

# --- Endpoints Binance -----------------------------------------------------
# api.binance.com est joignable depuis la France (verifie le 2026-08-28).
# data-api.binance.vision est le miroir public "market data only" : meme
# schema de reponse, sert de repli si l'hote principal devient inaccessible.
REST_BASE_URL = "https://api.binance.com"
REST_FALLBACK_URL = "https://data-api.binance.vision"
WS_BASE_URL = "wss://stream.binance.com:9443"

# --- Limites de l'API (relevees en direct via /api/v3/exchangeInfo) ---------
WEIGHT_LIMIT_PER_MINUTE = 6000   # REQUEST_WEIGHT / 1 min / IP
KLINES_MAX_PER_REQUEST = 1000    # une demande superieure est tronquee SANS erreur
KLINES_REQUEST_WEIGHT = 2        # cout constant, independant de `limit`

# Seuil a partir duquel on se met en pause plutot que de risquer un ban IP (418)
WEIGHT_PAUSE_THRESHOLD = int(WEIGHT_LIMIT_PER_MINUTE * 0.75)


def resolve_intervals(profiles: list[str]) -> dict[str, int | None]:
    """Retourne, pour les profils demandes, l'intervalle -> profondeur en jours.

    Un meme intervalle peut appartenir a plusieurs profils avec des profondeurs
    differentes (15m est demande sur 180 jours en scalping, 730 en day trading).
    On retient alors la profondeur la PLUS LONGUE : collecter une fois large
    satisfait les deux profils, alors que l'inverse laisserait un profil a court
    de donnees.
    """
    resolved: dict[str, int | None] = {}

    for name in profiles:
        if name not in TRADING_PROFILES:
            connus = ", ".join(TRADING_PROFILES)
            raise ValueError(f"Profil inconnu : {name!r}. Profils disponibles : {connus}")

        profile = TRADING_PROFILES[name]
        for interval in profile["intervals"]:
            depth = profile["history_days"]

            if interval not in resolved:
                resolved[interval] = depth
            elif resolved[interval] is not None:
                # None l'emporte toujours : c'est "tout l'historique".
                resolved[interval] = None if depth is None else max(resolved[interval], depth)

    return resolved
