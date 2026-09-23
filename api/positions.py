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

LE RATTRAPAGE
    Le carnet avance a chaque appel de l'interface, qui ne juge que la
    DERNIERE bougie. Page fermee ou Docker arrete pendant trois jours : trois
    jours de signaux jamais vus. `rattraper` rejoue donc, avant le suivi
    normal, toutes les bougies cloturees depuis la derniere evaluee (table
    carnet_suivi), avec exactement le moteur du rejeu historique. Un vrai bot
    tournerait en continu ; ici, c'est le premier regard qui remet le carnet
    a jour.

Rien n'est envoye a Binance : aucun argent reel n'est engage.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from src.database import postgres_connection
from src.preprocessing import INTERVAL_SECONDS

logger = logging.getLogger(__name__)

FRAIS_ALLER_RETOUR = 0.002
CAPITAL_SIMULE = 10_000.0
FRACTION_PAR_POSITION = 0.05     # 5 % du capital, comme dans le backtest
# Au-dela, un trou n'est pas rattrape d'un coup : le premier appel prendrait
# trop de temps. Le script scripts.rejouer_positions reste la pour les longs
# historiques.
RATTRAPAGE_MAX_JOURS = 30


def simuler(serie: pd.DataFrame, decisions: dict, volatilite: np.ndarray, largeur: float,
            horizon: int, interval: str, symbole: str, style: str,
            debut: int = 0, garder_ouverte: bool = False) -> list[dict]:
    """Deroule le temps bougie par bougie et rend les positions prises.

    Moteur commun au rejeu historique et au rattrapage : les memes regles
    partout, sinon le carnet melangerait deux facons de compter.

    serie       bougies CLOTUREES d'une paire et d'un pas de temps, triees,
                index 0..n-1
    decisions   {open_time iso: decision du modele}
    debut       premiere bougie autorisee a ouvrir une position
    garder_ouverte
                si la derniere position n'est pas soldee a la fin des donnees,
                la rendre avec le statut 'ouverte' (le suivi normal la fermera)
                au lieu de l'oublier
    """
    positions, i = [], debut
    while i < len(serie):
        bougie = serie.iloc[i]
        decision = decisions.get(bougie["open_time"].isoformat())
        if decision is None or decision["decision"] == "attendre" or not np.isfinite(volatilite[i]):
            i += 1
            continue

        sens = 1 if decision["decision"] == "acheter" else -1
        entree = float(bougie["close"])
        # Barrieres fixees A L'OUVERTURE, avec la volatilite connue a ce moment.
        distance = largeur * float(volatilite[i])
        take_profit = entree * (1 + distance) if sens == 1 else entree * (1 - distance)
        stop_loss = entree * (1 - distance) if sens == 1 else entree * (1 + distance)
        position = {
            "symbol": symbole, "interval": interval, "style": style, "sens": sens,
            "bougie_signal": bougie["open_time"], "prix_entree": entree,
            "take_profit": take_profit, "stop_loss": stop_loss,
            "echeance": bougie["open_time"]
                        + pd.Timedelta(seconds=INTERVAL_SECONDS[interval]) * (horizon + 1),
            "probabilite_hausse": decision["probabilite_hausse"],
            "statut": "ouverte", "prix_sortie": None, "fermee_a": None,
            "rendement_brut_pct": None, "rendement_net_pct": None,
        }

        statut, prix_sortie, ferme_a, j = None, None, None, i + 1
        while j < len(serie) and j <= i + horizon:
            suivante = serie.iloc[j]
            touche_stop = (suivante["low"] <= stop_loss if sens == 1
                           else suivante["high"] >= stop_loss)
            touche_gain = (suivante["high"] >= take_profit if sens == 1
                           else suivante["low"] <= take_profit)
            # Les deux dans la meme bougie : on retient la perte (prudence).
            if touche_stop:
                statut, prix_sortie, ferme_a = "stop loss", stop_loss, suivante["close_time"]
                break
            if touche_gain:
                statut, prix_sortie, ferme_a = "take profit", take_profit, suivante["close_time"]
                break
            j += 1

        if statut is None:
            if j >= len(serie):
                # Donnees epuisees avant l'issue : la position court encore.
                if garder_ouverte:
                    positions.append(position)
                break
            suivante = serie.iloc[j]
            statut, prix_sortie, ferme_a = "echeance", float(suivante["close"]), suivante["close_time"]

        brut = (prix_sortie / entree - 1) * sens
        position.update({
            "statut": statut, "prix_sortie": float(prix_sortie), "fermee_a": ferme_a,
            "rendement_brut_pct": round(brut * 100, 4),
            "rendement_net_pct": round((brut - FRAIS_ALLER_RETOUR) * 100, 4),
        })
        positions.append(position)
        i = j + 1                          # une seule position a la fois

    return positions


def inserer(cur, positions: list[dict]) -> int:
    """Ecrit des positions ; une bougie deja presente n'est jamais doublee."""
    from psycopg2.extras import execute_values

    if not positions:
        return 0
    colonnes = ["symbol", "interval", "style", "sens", "bougie_signal", "ouverte_a",
                "prix_entree", "take_profit", "stop_loss", "echeance", "probabilite_hausse",
                "statut", "prix_sortie", "fermee_a", "rendement_brut_pct", "rendement_net_pct"]
    # `ouverte_a` reprend la date de la bougie : la position est censee avoir
    # ete ouverte a ce moment-la, pas au moment du rejeu.
    lignes = [tuple(p["bougie_signal"] if c == "ouverte_a" else p[c] for c in colonnes)
              for p in positions]
    execute_values(cur, f"""
        INSERT INTO positions_virtuelles ({", ".join(colonnes)})
        VALUES %s
        ON CONFLICT (symbol, interval, style, bougie_signal) DO NOTHING
    """, lignes)
    return cur.rowcount


def _marquer(cur, symbole: str, interval: str, style: str, bougie) -> None:
    """Retient la derniere bougie evaluee (jamais de retour en arriere)."""
    cur.execute(
        """
        INSERT INTO carnet_suivi (symbol, interval, style, derniere_bougie_evaluee)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (symbol, interval, style) DO UPDATE
           SET derniere_bougie_evaluee = GREATEST(carnet_suivi.derniere_bougie_evaluee,
                                                  EXCLUDED.derniere_bougie_evaluee),
               mis_a_jour = now()
        """,
        (symbole, interval, style, bougie),
    )


def _repere(cur, symbole: str, interval: str, style: str):
    """D'ou repartir : la derniere bougie evaluee, ou a defaut la fin du carnet."""
    cur.execute(
        "SELECT derniere_bougie_evaluee FROM carnet_suivi "
        "WHERE symbol = %s AND interval = %s AND style = %s",
        (symbole, interval, style),
    )
    ligne = cur.fetchone()
    if ligne:
        return pd.Timestamp(ligne[0])
    # Carnet rempli avant l'existence du suivi : on repart de sa derniere trace.
    cur.execute(
        "SELECT max(coalesce(fermee_a, bougie_signal)) FROM positions_virtuelles "
        "WHERE symbol = %s AND interval = %s AND style = %s",
        (symbole, interval, style),
    )
    ligne = cur.fetchone()
    return pd.Timestamp(ligne[0]) if ligne and ligne[0] is not None else None


def bougies_a_rattraper(symbole: str, interval: str, style: str) -> int:
    """Nombre de bougies cloturees jamais evaluees (0 = carnet a jour ou vide)."""
    with postgres_connection() as conn, conn.cursor() as cur:
        repere = _repere(cur, symbole, interval, style)
    if repere is None:
        return 0
    ecart = (pd.Timestamp.now(tz="UTC") - repere).total_seconds() / INTERVAL_SECONDS[interval]
    # Moins de deux bougies : le suivi normal (derniere bougie) suffit.
    if ecart < 2:
        return 0
    plafond = RATTRAPAGE_MAX_JOURS * 86400 // INTERVAL_SECONDS[interval]
    return int(min(ecart, plafond))


def rattraper(bougies: pd.DataFrame, symbole: str, interval: str, style: str,
              largeur: float, horizon: int) -> dict:
    """Rejoue les bougies cloturees que personne n'a regardees."""
    from api import modele

    maintenant = pd.Timestamp.now(tz="UTC")
    serie = (bougies[(bougies["symbol"] == symbole) & (bougies["interval"] == interval)
                     & (bougies["close_time"] <= maintenant)]
             .sort_values("open_time").reset_index(drop=True))
    resultat = {"bougies_rejouees": 0, "positions_ajoutees": 0, "positions_fermees": 0}
    if serie.empty:
        return resultat

    with postgres_connection() as conn, conn.cursor() as cur:
        repere = _repere(cur, symbole, interval, style)

        # 1. Ce qui etait ouvert avant l'interruption se ferme d'abord, sur les
        #    vraies bougies de la periode manquee.
        fermees = sum(_fermer(cur, p, serie, maintenant)
                      for p in _ouvertes(cur, symbole, interval, style))
        resultat["positions_fermees"] = fermees

        # 2. Ensuite seulement, de nouvelles positions : jamais pendant qu'une
        #    autre court, ni avant la fermeture de la precedente.
        if not _ouvertes(cur, symbole, interval, style):
            cur.execute(
                "SELECT max(fermee_a) FROM positions_virtuelles "
                "WHERE symbol = %s AND interval = %s AND style = %s",
                (symbole, interval, style),
            )
            reperes = [t for t in (repere, cur.fetchone()[0]) if t is not None]
            depart = max(pd.Timestamp(t) for t in reperes) if reperes else None
            # Premiere bougie posterieure au depart = premiere a evaluer.
            debut = int((serie["open_time"] <= depart).sum()) if depart is not None else 0
            resultat["bougies_rejouees"] = len(serie) - debut

            if debut < len(serie):
                from api.ordre import FENETRE_VOLATILITE
                from src.labeling import volatilite_glissante

                decisions = {d["open_time"]: d for d in
                             modele.predire_serie(bougies, symbole, interval, style,
                                                  limite=len(serie))}
                volatilite = volatilite_glissante(serie["close"], FENETRE_VOLATILITE).to_numpy()
                nouvelles = simuler(serie, decisions, volatilite, largeur, horizon, interval,
                                    symbole, style, debut=debut, garder_ouverte=True)
                resultat["positions_ajoutees"] = inserer(cur, nouvelles)

        _marquer(cur, symbole, interval, style, serie["open_time"].iloc[-1])

    if resultat["bougies_rejouees"] or resultat["positions_fermees"]:
        logger.info("Rattrapage %s %s %s : %s", symbole, interval, style, resultat)
    return resultat


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

        _marquer(cur, symbole, interval, style, decision["bougie"])

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
                   take_profit, stop_loss, rendement_brut_pct, rendement_net_pct
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
        "take_profit": float(f["take_profit"]), "stop_loss": float(f["stop_loss"]),
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
