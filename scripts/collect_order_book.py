"""Collecte l'historique du carnet d'ordres et le resume par bougie.

POURQUOI LES FUTURES ET PAS LE SPOT
    L'API publique de Binance ne renvoie que le carnet ACTUEL : aucun
    historique. Binance publie en revanche des archives quotidiennes sur
    data.binance.vision, mais uniquement pour les contrats perpetuels
    (futures USD-M), pas pour le spot.

    C'est un indicateur de substitution, et il faut le dire en soutenance.
    Il reste defendable pour deux raisons :
      - les prix spot et futures d'une meme paire evoluent ensemble a
        quelques points de base pres, et les futures, plus liquides, ont
        tendance a mener le spot ;
      - le bot final pourra lire ce meme carnet futures en direct
        (endpoint /fapi/v1/depth) : la variable est donc reproductible en
        production, ce qui est la condition pour qu'elle serve a quelque chose.

CE QUE CONTIENNENT LES ARCHIVES (bookDepth)
    Une photo du carnet toutes les 30 secondes. Pour chaque photo, la
    profondeur CUMULEE jusqu'a -5, -4, -3, -2, -1, -0,2 % du prix (cote
    acheteurs) et +0,2 ... +5 % (cote vendeurs), en quantite et en dollars.

CE QU'ON EN TIRE, PAR BOUGIE
    Le desequilibre du carnet a chaque niveau :

        (acheteurs - vendeurs) / (acheteurs + vendeurs)

    +1 = que des acheteurs, -1 = que des vendeurs, 0 = equilibre. Il est
    sans echelle par construction, comme toutes nos variables.

    Pour chaque bougie on garde la MOYENNE sur la bougie et la DERNIERE photo
    avant sa cloture, plus la liquidite a +-1 % rapportee a sa moyenne recente.

    Point critique : une bougie ouverte a 12h00 recoit les photos de
    [12h00, 12h15[. La derniere est prise vers 12h14m57, AVANT la cloture.
    La cible (sens de la bougie suivante) commence a 12h15 : pas de fuite.

Usage :
    python -m scripts.collect_order_book
    python -m scripts.collect_order_book --pairs BTCUSDT --debut 2026-08-01
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import requests

from src import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("carnet")

URL = "https://data.binance.vision/data/futures/um/daily/bookDepth"
RAW = config.DATA_RAW / "order_book"
SORTIE = config.DATA_PROCESSED / "order_book"
RAPPORT = config.DOCS / "carnet_ordres_couverture.json"

# Periode de l'extrait fige day trading.
DEBUT, FIN = "2024-08-29", "2026-08-29"
PAS = {"15m": "15min", "1h": "1h", "4h": "4h"}
NIVEAUX = (0.2, 1.0, 2.0, 5.0)


def telecharger(symbole: str, jour: str) -> Path | None:
    """Telecharge l'archive d'un jour et verifie son empreinte SHA-256.

    Binance publie un fichier .CHECKSUM a cote de chaque archive : sans
    verification, un telechargement tronque serait lu comme un jour valide.
    """
    nom = f"{symbole}-bookDepth-{jour}.zip"
    chemin = RAW / symbole / nom
    if chemin.exists():
        return chemin

    reponse = requests.get(f"{URL}/{symbole}/{nom}", timeout=120)
    if reponse.status_code == 404:
        return None  # jour absent des archives
    reponse.raise_for_status()

    attendu = requests.get(f"{URL}/{symbole}/{nom}.CHECKSUM", timeout=60).text.split()[0]
    if hashlib.sha256(reponse.content).hexdigest() != attendu:
        raise ValueError(f"{nom} : empreinte invalide, telechargement corrompu")

    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(reponse.content)
    return chemin


def photos(chemin: Path) -> pd.DataFrame:
    """Une ligne par photo du carnet : desequilibres et liquidite."""
    with zipfile.ZipFile(chemin) as archive:
        brut = pd.read_csv(archive.open(archive.namelist()[0]))

    brut["timestamp"] = pd.to_datetime(brut["timestamp"], utc=True)
    # En dollars (notional) : comparable entre paires, contrairement aux
    # quantites (1 BTC ne vaut pas 1 XRP).
    large = brut.pivot_table(index="timestamp", columns="percentage",
                             values="notional", aggfunc="last")

    sortie = pd.DataFrame(index=large.index)
    for niveau in NIVEAUX:
        if -niveau not in large.columns or niveau not in large.columns:
            continue
        acheteurs, vendeurs = large[-niveau], large[niveau]
        sortie[f"desequilibre_{niveau:g}"] = (acheteurs - vendeurs) / (acheteurs + vendeurs)
    sortie["liquidite_1"] = large.get(-1.0) + large.get(1.0)
    return sortie


def par_bougie(instantanes: pd.DataFrame, pas: str) -> pd.DataFrame:
    """Resume les photos de chaque bougie. L'index devient open_time."""
    groupes = instantanes.groupby(instantanes.index.floor(PAS[pas]))
    colonnes = [c for c in instantanes.columns if c.startswith("desequilibre_")]
    resume = groupes[colonnes].mean().add_suffix("_moy")
    resume = resume.join(groupes[colonnes].last().add_suffix("_fin"))
    resume["liquidite_1"] = groupes["liquidite_1"].mean()
    resume["photos"] = groupes.size()
    return resume


def traiter(symbole: str, jour: str):
    chemin = telecharger(symbole, jour)
    if chemin is None:
        return symbole, jour, None
    instantanes = photos(chemin)
    return symbole, jour, {pas: par_bougie(instantanes, pas) for pas in PAS}


def main():
    parser = argparse.ArgumentParser(description="Historique du carnet d'ordres")
    parser.add_argument("--pairs", nargs="+", default=config.PAIRS)
    parser.add_argument("--debut", default=DEBUT)
    parser.add_argument("--fin", default=FIN)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    jours = [d.strftime("%Y-%m-%d") for d in pd.date_range(args.debut, args.fin, freq="D")]
    taches = [(s, j) for s in args.pairs for j in jours]
    log.info("%d paires x %d jours = %d archives", len(args.pairs), len(jours), len(taches))

    morceaux: dict[tuple[str, str], list[pd.DataFrame]] = {}
    absents: dict[str, list[str]] = {s: [] for s in args.pairs}
    debut = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futurs = [pool.submit(traiter, s, j) for s, j in taches]
        for n, futur in enumerate(as_completed(futurs), start=1):
            symbole, jour, resumes = futur.result()
            if resumes is None:
                absents[symbole].append(jour)
            else:
                for pas, df in resumes.items():
                    morceaux.setdefault((symbole, pas), []).append(df)
            if n % 250 == 0:
                log.info("  %d / %d archives (%.0f s)", n, len(taches), time.time() - debut)

    SORTIE.mkdir(parents=True, exist_ok=True)
    rapport = {"source": URL, "debut": args.debut, "fin": args.fin, "paires": {}}
    for (symbole, pas), dfs in sorted(morceaux.items()):
        df = pd.concat(dfs).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        # Liquidite rapportee a sa norme recente : "le carnet est-il plus
        # garni que d'habitude ?". Calculee apres concatenation, pour que la
        # moyenne glissante traverse les jours.
        df["liquidite_relative"] = df["liquidite_1"] / df["liquidite_1"].rolling(24).mean()
        df = df.drop(columns=["liquidite_1"])
        df.index.name = "open_time"
        df = df.reset_index()
        df.insert(0, "interval", pas)
        df.insert(0, "symbol", symbole)
        df.to_parquet(SORTIE / f"{symbole}_{pas}.parquet", index=False)

        info = rapport["paires"].setdefault(symbole, {"jours_absents": sorted(absents[symbole])})
        info[pas] = {"bougies": len(df),
                     "photos_moyennes_par_bougie": round(float(df["photos"].mean()), 1)}

    RAPPORT.write_text(json.dumps(rapport, indent=2), encoding="utf-8")
    for symbole in args.pairs:
        log.info("%-8s : %d jours absents sur %d", symbole, len(absents[symbole]), len(jours))
    log.info("Termine en %.0f s. Rapport : %s", time.time() - debut, RAPPORT)


if __name__ == "__main__":
    main()
