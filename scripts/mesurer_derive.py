"""Mesure la derive des donnees, paire par paire et pas de temps par pas de temps.

Meme calcul que la route /derive de l'API, mais en ligne de commande : c'est
cette version que l'ordonnanceur de l'etape 5 (Airflow) appellera chaque jour.

DEUX REGLES APPRISES EN CONSTRUISANT CETTE MESURE
    1. On ne compare que des choses comparables. La reference est choisie pour
       la paire ET le pas de temps : la taille moyenne d'un trade va de 0,005
       sur le BTC a 115,8 sur le XRP, et l'ATR d'une bougie 4h vaut quatre fois
       celui d'une bougie 15m.
    2. La fenetre se donne en JOURS. Trop courte, elle fait sortir en "derive
       forte" les variables lentes (moyenne mobile 100, contexte 4h) sans
       qu'aucun regime n'ait change.

Le resultat est FUSIONNE dans docs/derive_donnees.json : on garde l'historique
des mesures, car ce qui compte n'est pas la valeur d'un jour mais sa TENDANCE.

Usage :
    python -m scripts.mesurer_derive
    python -m scripts.mesurer_derive --pairs BTCUSDT --jours 120
    python -m scripts.mesurer_derive --source extrait   # sans base de donnees
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src import config
from src.features import FAMILLES, ajouter_contexte_lent, construire_groupes
from src.preprocessing import INTERVAL_SECONDS
from api import derive as module_derive

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("derive")

HISTORIQUE = config.DOCS / "derive_donnees.json"
INTERVALLES = ("15m", "1h", "4h")


def bougies_depuis_base(paire: str, bougies: int) -> pd.DataFrame:
    from api.donnees import dernieres_bougies
    return dernieres_bougies(paire, par_intervalle=bougies)


def bougies_depuis_extrait(paire: str, bougies: int) -> pd.DataFrame:
    """Repli sans base : les bougies recentes collectees pour les tests."""
    chemin = config.DATA_PROCESSED / "nouvelles_bougies" / "day_trading_recent.parquet"
    df = pd.read_parquet(chemin)
    df = df[df["symbol"] == paire]
    return (df.sort_values("open_time").groupby("interval", as_index=False)
              .tail(bougies).reset_index(drop=True))


def main():
    parser = argparse.ArgumentParser(description="Derive des donnees")
    parser.add_argument("--pairs", nargs="+", default=config.PAIRS)
    parser.add_argument("--jours", type=int, default=90,
                        help="Fenetre analysee, en jours (90 par defaut)")
    parser.add_argument("--source", choices=["base", "extrait"], default="base")
    args = parser.parse_args()

    lecture = bougies_depuis_base if args.source == "base" else bougies_depuis_extrait
    reference = module_derive.charger_reference()
    groupes, alertes = {}, []

    for paire in args.pairs:
        # On lit assez de bougies pour couvrir la fenetre du pas de temps le
        # plus fin, puis chaque mesure ne garde que le sien.
        bougies = min(int(args.jours * 86400 / INTERVAL_SECONDS["15m"]), 15_000)
        brut = lecture(paire, bougies)
        if brut.empty:
            log.warning("%s : aucune donnee", paire)
            continue
        variables = ajouter_contexte_lent(construire_groupes(brut, FAMILLES))

        for interval in INTERVALLES:
            resultat = module_derive.mesurer(variables, reference, paire, interval)
            if not resultat["bougies_analysees"]:
                continue
            cle = f"{paire}|{interval}"
            groupes[cle] = {k: v for k, v in resultat.items() if k != "detail"}
            groupes[cle]["variables_les_plus_derivantes"] = resultat["detail"][:3]
            marque = "  <-- A REGARDER" if resultat["variables_en_derive_forte"] else ""
            log.info("%-14s %5d bougies | PSI median %6.4f max %6.4f | fortes %2d moderees %2d%s",
                     cle, resultat["bougies_analysees"], resultat["psi_median"] or 0,
                     resultat["psi_maximum"] or 0, resultat["variables_en_derive_forte"],
                     resultat["variables_en_derive_moderee"], marque)
            if resultat["variables_en_derive_forte"]:
                alertes.append(cle)

    mesure = {
        "mesure_le": datetime.now(timezone.utc).isoformat(),
        "fenetre_jours": args.jours,
        "source": args.source,
        "reference": {"periode": reference.get("periode"), "lignes": reference.get("lignes")},
        "groupes_analyses": len(groupes),
        "groupes_en_derive_forte": alertes,
        "verdict": ("reentrainement conseille : " + ", ".join(alertes)) if alertes
                   else "stable : aucun groupe en derive forte",
        "detail_par_groupe": groupes,
    }
    log.info("VERDICT : %s", mesure["verdict"])

    historique = {"mesures": []}
    if HISTORIQUE.exists():
        historique = json.loads(HISTORIQUE.read_text(encoding="utf-8"))
        historique.setdefault("mesures", [])
    historique["mesures"].append({k: v for k, v in mesure.items() if k != "detail_par_groupe"})
    historique["derniere_mesure"] = mesure
    HISTORIQUE.write_text(json.dumps(historique, indent=2), encoding="utf-8")
    log.info("Historique : %s (%d mesures)", HISTORIQUE, len(historique["mesures"]))


if __name__ == "__main__":
    main()
