"""Lecture des bases pour l'API.

L'API ne lit QUE les vues par profil (`v_day_trading`...), jamais les tables :
elle n'a pas a savoir quel profil stocke physiquement quel pas de temps. C'est
exactement la raison d'etre de ces vues, decidee a l'etape 2.
"""
from __future__ import annotations

import logging

import pandas as pd

from src.database import mongo_client, postgres_connection
from src import config

logger = logging.getLogger(__name__)

VUES = {"scalping": "v_scalping", "day_trading": "v_day_trading", "swing": "v_swing"}
COLONNES = ["symbol", "interval", "open_time", "close_time", "open", "high", "low",
            "close", "volume", "quote_volume", "nb_trades", "taker_buy_base"]


def dernieres_bougies(symbole: str, profil: str = "day_trading",
                      par_intervalle: int = 300) -> pd.DataFrame:
    """Les N dernieres bougies de chaque pas de temps du profil.

    Tous les pas de temps sont ramenes, pas seulement celui demande : le
    contexte multi-echelles a besoin des bougies 1h et 4h pour enrichir une
    bougie 15m.
    """
    vue = VUES[profil]
    requete = f"""
        SELECT {', '.join(COLONNES)} FROM (
            SELECT {', '.join(COLONNES)},
                   ROW_NUMBER() OVER (PARTITION BY interval ORDER BY open_time DESC) AS rang
            FROM {vue} WHERE symbol = %s
        ) t WHERE rang <= %s
    """
    with postgres_connection() as conn:
        df = pd.read_sql(requete, conn, params=(symbole, par_intervalle))
    return df.sort_values(["interval", "open_time"]).reset_index(drop=True)


def bougies_binance(symbole: str, par_intervalle: int = 300,
                    inclure_en_cours: bool = False) -> pd.DataFrame:
    """Les dernieres bougies lues DIRECTEMENT chez Binance.

    La base n'est alimentee que lorsqu'on lance la collecte : elle a donc
    toujours du retard. Pour une interface qui doit montrer le marche tel
    qu'il est maintenant, on interroge Binance a la volee.

    La bougie EN COURS est ecartee par defaut : sa "cloture" n'est qu'un prix
    intermediaire, et le modele n'a jamais appris sur des bougies inachevees.
    `inclure_en_cours` la garde quand meme, pour l'AFFICHER - jamais pour
    predire dessus. Elle se reconnait a sa date de fermeture dans le futur.
    """
    from src.binance_rest import BinanceClient
    from src.preprocessing import normalize_klines

    client = BinanceClient()
    maintenant = pd.Timestamp.now(tz="UTC")
    morceaux = []
    for interval in ("15m", "1h", "4h"):
        brut = client.klines(symbole, interval, limit=min(par_intervalle, 1000))
        df = normalize_klines(brut, symbole, interval)
        morceaux.append(df if inclure_en_cours else df[df["close_time"] < maintenant])
    return pd.concat(morceaux, ignore_index=True)[COLONNES]


def couverture() -> list[dict]:
    """Etat des donnees : volume et retard de collecte, via la vue de l'etape 2."""
    with postgres_connection() as conn:
        df = pd.read_sql("SELECT * FROM v_coverage ORDER BY symbol, interval", conn)
    return df.to_dict("records")


def paires_disponibles() -> list[str]:
    with postgres_connection() as conn:
        df = pd.read_sql("SELECT symbol FROM symbols ORDER BY symbol", conn)
    return df["symbol"].tolist()


def journaliser_prediction(prediction: dict) -> None:
    """Archive la prediction, pour pouvoir mesurer la derive plus tard.

    Sans cette trace, impossible de repondre a "le modele s'est-il mis a
    acheter beaucoup plus qu'avant ?", qui est le premier signe d'une derive.
    L'echec d'ecriture ne doit jamais casser la reponse a l'utilisateur.
    """
    try:
        with postgres_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO api_predictions
                    (symbol, interval, style, bougie_open_time, probabilite_hausse,
                     seuil, decision)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (prediction["symbole"], prediction["interval"], prediction["style"],
                 prediction["bougie"], prediction["probabilite_hausse"],
                 prediction["seuil_du_style"], prediction["decision"]),
            )
    except Exception as exc:  # pragma: no cover - depend de la base
        logger.warning("prediction non journalisee (%s)", type(exc).__name__)


def etat_bases() -> dict[str, str]:
    """Les deux bases repondent-elles ?"""
    etats = {}
    try:
        with postgres_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        etats["postgresql"] = "ok"
    except Exception as exc:
        etats["postgresql"] = f"indisponible ({type(exc).__name__})"
    try:
        with mongo_client() as client:
            client[config.MONGO_DB_NAME].command("ping")
        etats["mongodb"] = "ok"
    except Exception as exc:
        etats["mongodb"] = f"indisponible ({type(exc).__name__})"
    return etats
