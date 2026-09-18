"""Le bot trade-t-il quand le marche s'agite ?

QUESTION POSEE
    Les grosses hausses sont les moments ou l'on gagnerait le plus. Le bot
    s'y abstient-il ? Et si oui, pourquoi ?

CE QU'ON MESURE
    On classe les bougies par volatilite recente (ATR relatif), en dix
    tranches, puis on regarde dans chacune :
      - la part de bougies ou le modele se prononce ;
      - la repartition achat / vente ;
      - la probabilite de hausse moyenne, pour voir s'il devient indecis ;
      - ce que les trades auraient rapporte, frais compris.

    On detaille ensuite les journees les plus agitees, pour verifier sur des
    dates precises.

Usage :
    python -m scripts.activite_selon_volatilite
    python -m scripts.activite_selon_volatilite --paire ETHUSDT --interval 15m
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src import config
from api import donnees, modele

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("activite")

RESULTATS = config.DOCS / "activite_selon_volatilite.json"
FRAIS = 0.002


def preparer(paire: str, interval: str, style: str, bougies_max: int = 1000) -> pd.DataFrame:
    """Une ligne par bougie : sa volatilite, la decision du modele, le resultat."""
    from src.features import FAMILLES, ajouter_contexte_lent, construire_groupes
    from src.preprocessing import INTERVAL_SECONDS

    bougies = donnees.bougies_binance(paire, bougies_max)
    decisions = modele.predire_serie(bougies, paire, interval, style, limite=bougies_max)
    if not decisions:
        raise SystemExit("aucune decision : pas assez de bougies")

    variables = ajouter_contexte_lent(construire_groupes(bougies, FAMILLES))
    variables = variables[(variables["symbol"] == paire) & (variables["interval"] == interval)]
    volatilite = variables.set_index("open_time")["atr_relatif"]

    serie = (bougies[(bougies["symbol"] == paire) & (bougies["interval"] == interval)]
             .sort_values("open_time").set_index("open_time"))
    rendement = serie["close"].pct_change().shift(-1)

    lignes = []
    for d in decisions:
        horodatage = pd.Timestamp(d["open_time"])
        if horodatage not in volatilite.index or not np.isfinite(volatilite.get(horodatage, np.nan)):
            continue
        suivant = rendement.get(horodatage, np.nan)
        lignes.append({
            "open_time": horodatage,
            "atr_relatif": float(volatilite[horodatage]),
            "probabilite": d["probabilite_hausse"],
            "decision": d["decision"],
            "rendement_suivant": float(suivant) if np.isfinite(suivant) else np.nan,
        })
    return pd.DataFrame(lignes)


def par_tranche(jeu: pd.DataFrame) -> pd.DataFrame:
    jeu = jeu.copy()
    jeu["tranche"] = pd.qcut(jeu["atr_relatif"], 10, labels=False, duplicates="drop")
    lignes = []
    for tranche, groupe in jeu.groupby("tranche"):
        agit = groupe[groupe["decision"] != "attendre"]
        sens = np.where(agit["decision"] == "acheter", 1.0, -1.0)
        brut = (agit["rendement_suivant"] * sens).mean() if len(agit) else np.nan
        lignes.append({
            "tranche": f"{int(tranche) + 1}/10",
            "volatilite_moyenne_pct": round(float(groupe["atr_relatif"].mean() * 100), 3),
            "bougies": len(groupe),
            "signaux": len(agit),
            "part_de_signaux_pct": round(100 * len(agit) / len(groupe), 1),
            "achats": int((agit["decision"] == "acheter").sum()),
            "ventes": int((agit["decision"] == "vendre").sum()),
            "probabilite_moyenne": round(float(groupe["probabilite"].mean()), 4),
            "ecart_type_probabilite": round(float(groupe["probabilite"].std()), 4),
            "gain_brut_moyen_pct": round(float(brut * 100), 4) if np.isfinite(brut) else None,
        })
    return pd.DataFrame(lignes).set_index("tranche")


def journees_agitees(jeu: pd.DataFrame, combien: int = 6) -> pd.DataFrame:
    jeu = jeu.copy()
    jeu["jour"] = jeu["open_time"].dt.date
    resume = jeu.groupby("jour").agg(
        bougies=("decision", "size"),
        volatilite_pct=("atr_relatif", lambda s: round(float(s.mean() * 100), 3)),
        amplitude_pct=("rendement_suivant", lambda s: round(float(s.abs().sum() * 100), 2)),
        signaux=("decision", lambda s: int((s != "attendre").sum())),
        achats=("decision", lambda s: int((s == "acheter").sum())),
        probabilite_moyenne=("probabilite", lambda s: round(float(s.mean()), 4)),
    )
    return resume.sort_values("volatilite_pct", ascending=False).head(combien)


def main():
    parser = argparse.ArgumentParser(description="Activite du bot selon la volatilite")
    parser.add_argument("--paire", default="BTCUSDT")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--style", default="conservateur")
    args = parser.parse_args()

    jeu = preparer(args.paire, args.interval, args.style)
    log.info("%s %s %s : %d bougies analysees, du %s au %s", args.paire, args.interval,
             args.style, len(jeu), jeu["open_time"].min().date(), jeu["open_time"].max().date())

    tranches = par_tranche(jeu)
    print()
    print(tranches.to_string())
    print()

    calme = tranches.iloc[:3]["part_de_signaux_pct"].mean()
    agite = tranches.iloc[-3:]["part_de_signaux_pct"].mean()
    log.info("Part de signaux : %.1f %% dans les bougies les plus CALMES contre %.1f %% "
             "dans les plus AGITEES", calme, agite)
    log.info("Ecart-type des probabilites : %.4f (calme) contre %.4f (agite) - un modele "
             "indecis colle a 0,5", tranches.iloc[:3]["ecart_type_probabilite"].mean(),
             tranches.iloc[-3:]["ecart_type_probabilite"].mean())

    print()
    print("Journees les plus agitees :")
    print(journees_agitees(jeu).to_string())

    RESULTATS.write_text(json.dumps({
        "paire": args.paire, "interval": args.interval, "style": args.style,
        "bougies": len(jeu),
        "par_tranche_de_volatilite": tranches.reset_index().to_dict("records"),
        "journees_les_plus_agitees": journees_agitees(jeu, 10).reset_index().astype(str).to_dict("records"),
    }, indent=2), encoding="utf-8")
    log.info("Resultats ecrits : %s", RESULTATS)


if __name__ == "__main__":
    main()
