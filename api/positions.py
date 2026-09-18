"""Carnet de positions virtuelles : le bot suit ce qu'il a ouvert.

POURQUOI CE MODULE EXISTE
    Jusqu'ici l'API jugeait chaque bougie independamment, sans rien memoriser.
    C'est la bonne facon de MESURER un modele - chaque bougie est un essai -
    mais pas de faire tourner un bot : sans etat, impossible de dire si un
    trade a rapporte quelque chose.

    C'est la difference entre une simulation vectorisee (on calcule tous les
    signaux d'un coup, utile pour comparer des modeles) et une simulation
    evenementielle (on rejoue le temps, position apres position, comme en
    production).

DEUX REGLES, REPRISES DU BACKTEST DE L'ETAPE 3
    1. UNE SEULE POSITION A LA FOIS par paire, pas de temps et style. Ouvrir
       a chaque bougie reviendrait a detenir des dizaines de positions
       simultanees avec un capital qu'on n'a pas - et les mesures seraient
       flattees par des trades qui se recouvrent.
    2. FRAIS AUX DEUX BOUTS : 0,1 % a l'entree, 0,1 % a la sortie.

COMMENT UNE POSITION SE FERME
    - le plus haut de la bougie touche le take profit -> gain ;
    - le plus bas touche le stop loss -> perte ;
    - l'echeance (12 bougies) arrive -> on solde au prix de cloture.

    Si les deux barrieres sont touchees dans la MEME bougie, on retient le
    stop loss : c'est l'hypothese prudente, puisque les donnees ne disent pas
    dans quel ordre les prix ont ete atteints.

Rien n'est envoye a Binance : aucun argent reel n'est engage.
"""
from __future__ import annotations

import logging

import pandas as pd

from src.database import postgres_connection

logger = logging.getLogger(__name__)

FRAIS_ALLER_RETOUR = 0.002
CAPITAL_SIMULE = 10_000.0
FRACTION_PAR_POSITION = 0.05     # 5 % du capital, comme dans le backtest


def _ouvertes(cur, symbole: str, interval: str, style: str) -> list[dict]:
    cur.execute(
        """
        SELECT position_id, sens, bougie_signal, prix_entree, take_profit, stop_loss, echeance
        FROM positions_virtuelles
        WHERE symbol = %s AND interval = %s AND style = %s AND statut = 'ouverte'
        ORDER BY bougie_signal
        """,
        (symbole, interval, style),
    )
    colonnes = [c[0] for c in cur.description]
    return [dict(zip(colonnes, ligne)) for ligne in cur.fetchall()]


def _fermer(cur, position: dict, bougies: pd.DataFrame, maintenant: pd.Timestamp) -> bool:
    """Rejoue les bougies posterieures au signal et ferme si une barriere tombe."""
    suite = bougies[bougies["open_time"] > position["bougie_signal"]].sort_values("open_time")
    sens = position["sens"]

    for _, bougie in suite.iterrows():
        touche_stop = (bougie["low"] <= position["stop_loss"] if sens == 1
                       else bougie["high"] >= position["stop_loss"])
        touche_gain = (bougie["high"] >= position["take_profit"] if sens == 1
                       else bougie["low"] <= position["take_profit"])

        if touche_stop or touche_gain:
            # Hypothese prudente : si les deux tombent dans la meme bougie,
            # on retient la perte.
            statut = "stop loss" if touche_stop else "take profit"
            prix = position["stop_loss"] if touche_stop else position["take_profit"]
            _enregistrer_fermeture(cur, position, statut, prix, bougie["close_time"])
            return True

        if bougie["open_time"] >= position["echeance"]:
            _enregistrer_fermeture(cur, position, "echeance", float(bougie["close"]),
                                   bougie["close_time"])
            return True

    # L'echeance peut tomber sans qu'aucune bougie ne la depasse encore.
    if maintenant > position["echeance"] and not suite.empty:
        derniere = suite.iloc[-1]
        _enregistrer_fermeture(cur, position, "echeance", float(derniere["close"]),
                               derniere["close_time"])
        return True
    return False


def _enregistrer_fermeture(cur, position: dict, statut: str, prix: float, quand) -> None:
    brut = (prix / position["prix_entree"] - 1) * position["sens"]
    net = brut - FRAIS_ALLER_RETOUR
    cur.execute(
        """
        UPDATE positions_virtuelles
           SET statut = %s, prix_sortie = %s, fermee_a = %s,
               rendement_brut_pct = %s, rendement_net_pct = %s
         WHERE position_id = %s
        """,
        (statut, float(prix), quand, round(brut * 100, 4), round(net * 100, 4),
         position["position_id"]),
    )


def synchroniser(bougies: pd.DataFrame, symbole: str, interval: str, style: str,
                 decision: dict, ordre: dict) -> dict:
    """Ferme ce qui doit l'etre, puis ouvre si le modele donne un signal."""
    serie = (bougies[(bougies["symbol"] == symbole) & (bougies["interval"] == interval)]
             .sort_values("open_time"))
    maintenant = pd.Timestamp.now(tz="UTC")
    fermees, ouverte = 0, False

    with postgres_connection() as conn, conn.cursor() as cur:
        for position in _ouvertes(cur, symbole, interval, style):
            if _fermer(cur, position, serie, maintenant):
                fermees += 1

        if decision["sens"] != 0 and not _ouvertes(cur, symbole, interval, style):
            # ON CONFLICT : la meme bougie ne doit pas rouvrir une position
            # quand l'interface se rafraichit toutes les 30 secondes.
            cur.execute(
                """
                INSERT INTO positions_virtuelles
                    (symbol, interval, style, sens, bougie_signal, prix_entree,
                     take_profit, stop_loss, echeance, probabilite_hausse)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, interval, style, bougie_signal) DO NOTHING
                """,
                (symbole, interval, style, decision["sens"], decision["bougie"],
                 ordre["entree"], ordre["take_profit"], ordre["stop_loss"],
                 ordre["echeance"], decision["probabilite_hausse"]),
            )
            ouverte = cur.rowcount > 0

    return {"positions_fermees": fermees, "position_ouverte": ouverte}


def etat(symbole: str, interval: str, style: str, prix_actuel: float) -> dict:
    """Positions ouvertes (avec leur gain en cours), historique et bilan."""
    with postgres_connection() as conn:
        ouvertes = pd.read_sql(
            """
            SELECT position_id, sens, bougie_signal, prix_entree, take_profit, stop_loss,
                   echeance, probabilite_hausse
            FROM positions_virtuelles
            WHERE symbol = %s AND interval = %s AND style = %s AND statut = 'ouverte'
            ORDER BY bougie_signal DESC
            """, conn, params=(symbole, interval, style))
        fermees = pd.read_sql(
            """
            SELECT sens, bougie_signal, fermee_a, prix_entree, prix_sortie, statut,
                   rendement_brut_pct, rendement_net_pct
            FROM positions_virtuelles
            WHERE symbol = %s AND interval = %s AND style = %s AND statut <> 'ouverte'
            ORDER BY fermee_a DESC LIMIT 50
            """, conn, params=(symbole, interval, style))
        bilan = pd.read_sql(
            """
            SELECT statut, COUNT(*) AS nombre, AVG(rendement_net_pct) AS moyenne
            FROM positions_virtuelles
            WHERE symbol = %s AND interval = %s AND style = %s AND statut <> 'ouverte'
            GROUP BY statut
            """, conn, params=(symbole, interval, style))

    en_cours = []
    for _, p in ouvertes.iterrows():
        brut = (prix_actuel / p["prix_entree"] - 1) * p["sens"]
        en_cours.append({
            "sens": int(p["sens"]),
            "ouverte_sur_la_bougie": p["bougie_signal"].isoformat(),
            "prix_entree": float(p["prix_entree"]),
            "take_profit": float(p["take_profit"]),
            "stop_loss": float(p["stop_loss"]),
            "echeance": p["echeance"].isoformat(),
            "prix_actuel": prix_actuel,
            # Gain "sur le papier" : ce qu'on toucherait en soldant maintenant.
            "gain_en_cours_brut_pct": round(float(brut) * 100, 3),
            "gain_en_cours_net_pct": round(float(brut - FRAIS_ALLER_RETOUR) * 100, 3),
        })

    historique = [{
        "sens": int(f["sens"]),
        "ouverte_sur_la_bougie": f["bougie_signal"].isoformat(),
        "fermee_a": f["fermee_a"].isoformat() if pd.notna(f["fermee_a"]) else None,
        "prix_entree": float(f["prix_entree"]),
        "prix_sortie": float(f["prix_sortie"]) if pd.notna(f["prix_sortie"]) else None,
        "statut": f["statut"],
        "rendement_net_pct": float(f["rendement_net_pct"]) if pd.notna(f["rendement_net_pct"]) else None,
    } for _, f in fermees.iterrows()]

    total = int(bilan["nombre"].sum()) if not bilan.empty else 0
    gagnantes = len([h for h in historique if (h["rendement_net_pct"] or 0) > 0])
    capital = CAPITAL_SIMULE
    # Les positions sont rejouees de la plus ancienne a la plus recente : le
    # capital compose comme dans le backtest de l'etape 3.
    for h in reversed(historique):
        if h["rendement_net_pct"] is not None:
            capital *= 1 + FRACTION_PAR_POSITION * h["rendement_net_pct"] / 100

    return {
        "ouvertes": en_cours,
        "historique": historique,
        "bilan": {
            "positions_fermees": total,
            "par_issue": {ligne["statut"]: int(ligne["nombre"]) for _, ligne in bilan.iterrows()},
            "gagnantes": gagnantes,
            "taux_de_reussite_pct": round(100 * gagnantes / len(historique), 1) if historique else None,
            "gain_moyen_net_pct": round(float(fermees["rendement_net_pct"].mean()), 3)
                                  if not fermees.empty else None,
            "capital_simule": round(capital, 2),
            "capital_initial": CAPITAL_SIMULE,
            "fraction_par_position_pct": FRACTION_PAR_POSITION * 100,
        },
    }
