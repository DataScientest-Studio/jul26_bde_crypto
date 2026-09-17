"""Le modele peut-il annoncer une GROSSE bougie verte ou rouge ?

La question posee : en regardant la FORME des dernieres bougies (corps, meches,
englobantes, enchainements), peut-on savoir si la prochaine sera une grosse
hausse, une grosse baisse, ou rien de notable ?

C'est different de tout ce qu'on a teste jusqu'ici :

  - la cible n'est plus "monte ou baisse" mais TROIS classes, dont deux qui
    rapportent assez pour couvrir les frais ;
  - on ajoute des variables de FORME. Jusqu'ici le modele ne recevait que des
    resumes chiffres (RSI, MACD, ecarts aux moyennes) : il ne voyait ni le
    corps d'une bougie, ni ses meches, ni un enchainement de trois bougies.

SEUIL DES "GROSSES" BOUGIES
    Le 10 % des mouvements les plus amples, mesure PAR PAS DE TEMPS et
    UNIQUEMENT sur la periode d'entrainement (sinon le seuil contiendrait deja
    une information sur la periode de test).

CE QU'ON MESURE
    - precision : quand le modele annonce une grosse hausse, combien de fois
      a-t-il raison ? A comparer au taux de base (environ 5 %).
    - valeur en argent : si on ne trade que ces annonces, le gain moyen
      couvre-t-il les 0,2 % de frais ?

Usage :
    python -m scripts.grosses_bougies
"""
from __future__ import annotations

import json
import logging
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src import config
from scripts.direction_prochaine_bougie import preparer_depuis
from scripts.pistes_amelioration import RECENTES, colonnes_de
from scripts.profils_de_risque import wilson

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("grosses")

RESULTATS = config.DOCS / "grosses_bougies.json"
FRAIS = 0.002
PART_GROSSES = 0.10          # les 10 % de mouvements les plus amples
CLASSES = {0: "grosse baisse", 1: "rien de notable", 2: "grosse hausse"}


def formes(brut: pd.DataFrame) -> pd.DataFrame:
    """Variables de FORME : ce qu'un trader voit sur le graphique.

    Toutes sont sans echelle (rapportees au prix ou a l'amplitude), et
    calculees sur la bougie courante et les deux precedentes : c'est la que se
    trouvent les figures classiques (englobante, marteau, doji).
    """
    morceaux = []
    for (symbole, pas), g in brut.groupby(["symbol", "interval"], sort=False):
        g = g.sort_values("open_time")
        haut, bas, ouv, clo = g["high"], g["low"], g["open"], g["close"]
        amplitude = (haut - bas).replace(0, np.nan)
        corps = clo - ouv

        f = pd.DataFrame({"symbol": symbole, "interval": pas,
                          "open_time": g["open_time"].to_numpy()})
        f["forme_corps"] = (corps / clo).to_numpy()                       # vert > 0, rouge < 0
        f["forme_corps_sur_amplitude"] = (corps.abs() / amplitude).to_numpy()  # 1 = pas de meche
        f["forme_meche_haute"] = ((haut - np.maximum(ouv, clo)) / amplitude).to_numpy()
        f["forme_meche_basse"] = ((np.minimum(ouv, clo) - bas) / amplitude).to_numpy()
        f["forme_amplitude"] = (amplitude / clo).to_numpy()

        # Les deux bougies precedentes : c'est ce qui fait une "figure".
        for k in (1, 2):
            f[f"forme_corps_t-{k}"] = (corps / clo).shift(k).to_numpy()
            f[f"forme_amplitude_t-{k}"] = (amplitude / clo).shift(k).to_numpy()
            f[f"forme_corps_sur_amplitude_t-{k}"] = (corps.abs() / amplitude).shift(k).to_numpy()

        # Englobante : le corps actuel avale le precedent, dans l'autre sens.
        corps_precedent = corps.shift(1)
        f["forme_englobante"] = ((corps.abs() > corps_precedent.abs())
                                 & (np.sign(corps) != np.sign(corps_precedent))
                                 ).astype(float).to_numpy()
        # Enchainement : combien des 3 dernieres bougies sont vertes ?
        f["forme_vertes_sur_3"] = (corps > 0).rolling(3).sum().to_numpy()
        # Amplitude de la bougie par rapport aux 24 precedentes : le marche
        # s'agite-t-il deja ?
        f["forme_amplitude_relative"] = (amplitude / amplitude.rolling(24).mean()).to_numpy()
        morceaux.append(f)
    return pd.concat(morceaux, ignore_index=True)


def etiqueter(jeu: pd.DataFrame, seuils: dict) -> pd.Series:
    """0 = grosse baisse, 1 = rien de notable, 2 = grosse hausse."""
    limite = jeu["interval"].map(seuils)
    return np.select([jeu["rendement_suivant"] <= -limite,
                      jeu["rendement_suivant"] >= limite], [0, 2], default=1)


def construire(brut: pd.DataFrame) -> pd.DataFrame:
    jeu = preparer_depuis(brut, contexte=True)
    jeu = jeu.merge(formes(brut), on=["symbol", "interval", "open_time"], how="left")
    return jeu.sort_values("open_time").reset_index(drop=True)


def mesurer(nom: str, modele, jeu: pd.DataFrame, colonnes: list[str], seuils: dict) -> dict:
    y = etiqueter(jeu, seuils)
    proba = modele.predict_proba(jeu[colonnes])
    prediction = proba.argmax(axis=1)
    rendements = jeu["rendement_suivant"].to_numpy()

    log.info("  --- %s (%d bougies) ---", nom, len(jeu))
    sortie = {"bougies": len(jeu), "classes": {}}
    for classe, libelle in CLASSES.items():
        taux_de_base = float((y == classe).mean())
        annonces = prediction == classe
        if annonces.sum() == 0:
            log.info("  %-15s taux de base %5.1f %% | jamais annoncee", libelle, taux_de_base * 100)
            sortie["classes"][libelle] = {"taux_de_base": round(taux_de_base, 4), "annonces": 0}
            continue
        justes = int((y[annonces] == classe).sum())
        bas, haut = wilson(justes, int(annonces.sum()))
        precision = justes / annonces.sum()
        sortie["classes"][libelle] = {
            "taux_de_base": round(taux_de_base, 4),
            "annonces": int(annonces.sum()),
            "precision": round(float(precision), 4),
            "ic95": [round(bas, 4), round(haut, 4)],
            "rappel": round(float(justes / max((y == classe).sum(), 1)), 4),
            # Quand il annonce une grosse bougie, detecte-t-il au moins
            # l'AGITATION, meme s'il se trompe de couleur ?
            "part_de_grosses_bougies": round(float((y[annonces] != 1).mean()), 4),
        }
        log.info("  %-15s taux de base %5.1f %% | %5d annonces | precision %.4f [%.3f-%.3f] | %s",
                 libelle, taux_de_base * 100, int(annonces.sum()), precision, bas, haut,
                 "MIEUX que le hasard" if bas > taux_de_base else "pas mieux que le hasard")

    # Valeur en argent : on ne trade que les annonces de grosse bougie.
    agit = prediction != 1
    if agit.sum():
        sens = np.where(prediction[agit] == 2, 1.0, -1.0)
        brut = float((rendements[agit] * sens).mean())
        justes_sens = float(((rendements[agit] > 0) == (sens > 0)).mean())
        sortie["si_on_trade_les_annonces"] = {
            "ordres": int(agit.sum()),
            "bon_sens": round(justes_sens, 4),
            "mouvement_moyen_pct": round(float(np.abs(rendements[agit]).mean() * 100), 4),
            "gain_brut_pct": round(brut * 100, 4),
            "gain_net_pct": round((brut - FRAIS) * 100, 4),
        }
        log.info("  en tradant ces %d annonces : bon sens %.4f | mouvement moyen %.3f %% | "
                 "brut %+.4f %% | net %+.4f %%", int(agit.sum()), justes_sens,
                 np.abs(rendements[agit]).mean() * 100, brut * 100, (brut - FRAIS) * 100)
    return sortie


def main():
    debut = time.time()
    extrait = pd.read_parquet(config.ROOT / "data" / "extract" / "day_trading.parquet")
    jeu = construire(extrait)
    colonnes = [c for c in colonnes_de(jeu) if c not in {"rendement_suivant", "label"}]
    formes_ajoutees = [c for c in colonnes if c.startswith("forme_")]

    coupure = int(len(jeu) * 0.80)
    apprentissage, test = jeu.iloc[:coupure], jeu.iloc[coupure:]

    # Seuil des "grosses" bougies : calcule sur l'ENTRAINEMENT seulement.
    seuils = (apprentissage.groupby("interval")["rendement_suivant"]
              .apply(lambda r: r.abs().quantile(1 - PART_GROSSES)).to_dict())
    log.info("%d variables dont %d de forme | seuils des grosses bougies : %s",
             len(colonnes), len(formes_ajoutees),
             {k: f"{v * 100:.2f} %" for k, v in seuils.items()})

    y_train = etiqueter(apprentissage, seuils)
    log.info("repartition a l'entrainement : %s",
             {CLASSES[c]: f"{(y_train == c).mean() * 100:.1f} %" for c in CLASSES})

    modele = Pipeline([
        ("imputation", SimpleImputer(strategy="median")),
        ("normalisation", StandardScaler()),
        # class_weight equilibre : sans cela, annoncer "rien de notable" a
        # chaque bougie donnerait deja 80 % de reussite.
        ("modele", HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05,
                                                  class_weight="balanced", random_state=0)),
    ])
    modele.fit(apprentissage[colonnes], y_train)
    log.info("Modele entraine en %.0f s", time.time() - debut)

    recentes = construire(pd.read_parquet(RECENTES))
    recentes = recentes[recentes["open_time"] > extrait["open_time"].max()].reset_index(drop=True)

    resultats = {
        "seuils_grosses_bougies": {k: round(v, 5) for k, v in seuils.items()},
        "variables_de_forme": formes_ajoutees,
        "test_historique": mesurer("test historique", modele, test, colonnes, seuils),
        "bougies_jamais_vues": mesurer("bougies jamais vues", modele, recentes, colonnes, seuils),
    }
    RESULTATS.write_text(json.dumps(resultats, indent=2), encoding="utf-8")
    log.info("Resultats ecrits : %s (%.0f s)", RESULTATS, time.time() - debut)


if __name__ == "__main__":
    main()
