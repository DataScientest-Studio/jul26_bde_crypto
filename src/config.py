"""Configuration centrale du projet.

Tout ce qui est "parametre du projet" vit ici et nulle part ailleurs :
changer de paires ou d'intervalle ne doit jamais demander de toucher au
code de collecte. C'est ce qui rend la fonction de recuperation generique.
"""
from pathlib import Path

# --- Chemins ---------------------------------------------------------------
RACINE = Path(__file__).resolve().parent.parent
DATA_RAW = RACINE / "data" / "raw"
DATA_PROCESSED = RACINE / "data" / "processed"
SAMPLES = RACINE / "samples"

# --- Perimetre fonctionnel -------------------------------------------------
# 5 paires majeures cotees en USDT. Choix justifie dans docs/rapport_etape1.md
PAIRES = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]

INTERVALLE = "1h"

# Debut d'historique : SOLUSDT n'existe que depuis 2020-08-11, c'est la paire
# contraignante. On aligne toutes les paires sur cette date pour disposer d'un
# axe temporel commun, indispensable pour toute feature cross-paires (etape 3).
DEBUT_HISTORIQUE = "2020-09-01"

# --- Endpoints Binance -----------------------------------------------------
# api.binance.com est joignable depuis la France (verifie le 2026-08-28).
# data-api.binance.vision est le miroir public "market data only" : meme
# schema de reponse, sert de repli si l'hote principal devient inaccessible.
BASE_REST = "https://api.binance.com"
BASE_REST_REPLI = "https://data-api.binance.vision"
BASE_WS = "wss://stream.binance.com:9443"

# --- Limites de l'API (relevees en direct via /api/v3/exchangeInfo) ---------
LIMITE_POIDS_MINUTE = 6000   # REQUEST_WEIGHT / 1 min / IP
KLINES_MAX_PAR_REQUETE = 1000  # une demande superieure est tronquee SANS erreur
POIDS_REQUETE_KLINES = 2       # cout constant, independant de `limit`

# Seuil a partir duquel on se met en pause plutot que de risquer un ban IP (418)
SEUIL_POIDS_PAUSE = int(LIMITE_POIDS_MINUTE * 0.75)
