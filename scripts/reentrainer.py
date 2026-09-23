"""Reentrainement automatique : champion contre challenger.

Appele chaque semaine par Airflow (et plus tot si la derive devient forte),
en trois etapes independantes - une tache Airflow chacune :

    figer       photographie les bougies de la base dans un extrait date et
                signe (SHA-256). La regle de Remy tient toujours : on
                n'entraine JAMAIS sur des donnees vivantes, seulement sur un
                extrait fige, relu et verifie.
    entrainer   meme recette que le modele final de l'etape 3
                (scripts/train_direction_final.py), sur une fenetre glissante
                de 2 ans. Puis le DUEL : le modele en service (champion) et le
                nouveau (challenger) sont mesures sur les 21 derniers jours,
                que le challenger n'a jamais vus.
    publier     seulement si le challenger gagne : il remplace le champion
                (en une operation atomique), la reference de derive est
                regeneree, et MLflow deplace l'alias "champion".

POURQUOI UN DUEL PLUTOT QUE "LE PLUS RECENT EST LE MEILLEUR"
    Un reentrainement peut rendre un modele moins bon : marche atypique
    pendant la fenetre, donnees manquantes, bug introduit dans le code. Sans
    comparaison, la production se degraderait en silence. Avec, le pire cas
    est "on garde le modele actuel".

LA REGLE DE PROMOTION
    La mesure qui compte est celle du produit : l'accuracy des ordres du
    style conservateur (le bouton par defaut). Le challenger est promu si :
      1. il a passe au moins MIN_ORDRES ordres sur la periode (en dessous,
         la mesure est du bruit) ;
      2. son accuracy est au moins egale a celle du champion.
    Un champion illisible (fichier absent, variables incompatibles) est
    remplace d'office.

Usage :
    python -m scripts.reentrainer figer     --date 2026-09-27
    python -m scripts.reentrainer entrainer --date 2026-09-27
    python -m scripts.reentrainer publier   --date 2026-09-27
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src import config

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reentrainement")

DOSSIER = config.ROOT / "data" / "reentrainement"
MODELES = config.ROOT / "models"
CHAMPION = MODELES / "direction_day_trading.joblib"
REFERENCE_DERIVE = MODELES / "reference_derive.json"
ARCHIVES = MODELES / "archives"

FENETRE_JOURS = config.TRADING_PROFILES["day_trading"]["history_days"]   # 730
EVALUATION_JOURS = 21
# Une journee d'ecart entre l'apprentissage et l'evaluation : l'etiquette
# d'une bougie depend de la suivante, la derniere bougie d'apprentissage
# "verrait" sinon le debut de la periode d'evaluation.
PURGE = pd.Timedelta(days=1)
MIN_ORDRES = 30
STYLE_DE_DECISION = "conservateur"

EXPERIENCE = "cryptobot_reentrainement"
NOM_MODELE = "cryptobot-direction"
TYPES_SURS = [
    "numpy.dtype",
    "sklearn.calibration._CalibratedClassifier",
    "sklearn.isotonic.IsotonicRegression",
    "sklearn.frozen._frozen.FrozenEstimator",
    "sklearn.preprocessing._label.LabelEncoder",
    # Les arbres de la foret : types de scikit-learn lui-meme. skops 0.15
    # ne les accepte plus sans declaration explicite.
    "sklearn.tree._tree.Tree",
]


def dossier_du(date: str) -> Path:
    return DOSSIER / date


# ---------------------------------------------------------------------------
#  Regle de promotion - fonction pure, verifiee par les tests
# ---------------------------------------------------------------------------

def decider_promotion(champion: dict | None, challenger: dict) -> tuple[str, str]:
    """('promu' | 'rejete', raison) a partir des mesures des deux modeles.

    Chaque mesure : {"ordres": int, "accuracy": float} sur le style decisif.
    """
    ordres = challenger.get("ordres", 0)
    if ordres < MIN_ORDRES:
        return "rejete", (f"challenger : {ordres} ordres seulement, moins que les "
                          f"{MIN_ORDRES} necessaires pour juger")
    if champion is None or not champion.get("ordres"):
        return "promu", "aucun champion mesurable : le challenger prend la place"
    if challenger["accuracy"] >= champion["accuracy"]:
        return "promu", (f"accuracy {challenger['accuracy']:.4f} >= champion "
                         f"{champion['accuracy']:.4f}")
    return "rejete", (f"accuracy {challenger['accuracy']:.4f} < champion "
                      f"{champion['accuracy']:.4f} : on garde le modele en service")


# ---------------------------------------------------------------------------
#  1. Figer
# ---------------------------------------------------------------------------

def figer(date: str) -> dict:
    """Extrait date et signe des bougies day trading."""
    from src.database import postgres_connection
    from scripts.make_extract import empreinte, extraire

    dossier = dossier_du(date)
    dossier.mkdir(parents=True, exist_ok=True)
    with postgres_connection(autocommit=True) as conn:
        with conn.cursor() as cur:
            # Coupure = derniere bougie presente pour TOUTES les series :
            # au-dela, certaines seraient tronquees et d'autres non.
            cur.execute("""SELECT min(derniere) FROM (
                               SELECT max(open_time) AS derniere FROM v_day_trading
                                GROUP BY symbol, interval) s""")
            coupure = pd.Timestamp(cur.fetchone()[0])
        bougies = extraire(conn, "day_trading", coupure)

    bougies = bougies[bougies["open_time"] > coupure - pd.Timedelta(days=FENETRE_JOURS)]
    chemin = dossier / "day_trading.parquet"
    bougies.to_parquet(chemin, index=False)
    manifeste = {
        "cree_le": datetime.now(timezone.utc).isoformat(),
        "coupure": coupure.isoformat(),
        "fenetre_jours": FENETRE_JOURS,
        "lignes": len(bougies),
        "debut": str(bougies["open_time"].min()),
        "sha256": empreinte(chemin),
    }
    (dossier / "manifest.json").write_text(json.dumps(manifeste, indent=2), encoding="utf-8")
    log.info("Extrait fige : %d bougies, %s -> %s, sha256 %s", len(bougies),
             manifeste["debut"][:10], coupure.date(), manifeste["sha256"][:16])
    return manifeste


# ---------------------------------------------------------------------------
#  2. Entrainer et comparer
# ---------------------------------------------------------------------------

def mesurer(modele, colonnes: list[str], seuils: dict, evaluation: pd.DataFrame,
            jours: float) -> dict:
    from scripts.train_direction_final import mesurer_deux_seuils

    probabilites = modele.predict_proba(evaluation[colonnes])[:, 1]
    return mesurer_deux_seuils(probabilites, evaluation["label"].to_numpy(),
                               evaluation["rendement_suivant"].to_numpy(), seuils, jours)


def mesurer_champion(evaluation: pd.DataFrame, jours: float) -> tuple[dict | None, dict]:
    """Le modele en service, sur la meme periode. None s'il est illisible."""
    import joblib

    if not CHAMPION.exists():
        return None, {}
    paquet = joblib.load(CHAMPION)
    manquantes = [c for c in paquet["colonnes"] if c not in evaluation.columns]
    if manquantes:
        log.warning("Champion incompatible : variables absentes %s", manquantes[:5])
        return None, paquet
    mesures = {style: mesurer(paquet["modele"], paquet["colonnes"], seuils, evaluation, jours)
               for style, seuils in paquet["styles"].items()}
    return mesures, paquet


def entrainer(date: str) -> dict:
    import joblib

    from scripts.pistes_amelioration import (
        STYLES, colonnes_de, construire, construire_colonnes_seules, entrainer as ajuster,
    )
    from scripts.train_direction_final import seuils_par_cote

    dossier = dossier_du(date)
    manifeste = json.loads((dossier / "manifest.json").read_text(encoding="utf-8"))
    from scripts.make_extract import empreinte
    if empreinte(dossier / "day_trading.parquet") != manifeste["sha256"]:
        raise SystemExit("Extrait modifie depuis qu'il a ete fige : entrainement refuse.")

    debut = time.time()
    jeu = construire(pd.read_parquet(dossier / "day_trading.parquet"), set())
    colonnes = colonnes_de(construire_colonnes_seules(jeu, set()))

    coupure = pd.Timestamp(manifeste["coupure"])
    debut_evaluation = coupure - pd.Timedelta(days=EVALUATION_JOURS)
    evaluation = jeu[jeu["open_time"] > debut_evaluation].reset_index(drop=True)
    developpement = jeu[jeu["open_time"] <= debut_evaluation - PURGE].reset_index(drop=True)
    jours = EVALUATION_JOURS

    # Meme decoupage que le modele final de l'etape 3 : 72 / 8 / 20.
    n = len(developpement)
    i72, i80 = int(n * 0.72), int(n * 0.80)
    apprentissage = developpement.iloc[:i72]
    calibration = developpement.iloc[i72:i80]
    periode_seuils = developpement.iloc[i80:]
    log.info("apprentissage %d | calibration %d | seuils %d | evaluation %d (%d jours)",
             len(apprentissage), len(calibration), len(periode_seuils), len(evaluation), jours)

    modele = ajuster(apprentissage[colonnes], apprentissage["label"],
                     calibration[colonnes], calibration["label"])
    probabilites_seuils = modele.predict_proba(periode_seuils[colonnes])[:, 1]
    styles = {style: seuils_par_cote(probabilites_seuils, niveau)
              for style, niveau in STYLES.items()}
    log.info("Challenger entraine en %.0f s", time.time() - debut)

    mesures_challenger = {style: mesurer(modele, colonnes, seuils, evaluation, jours)
                          for style, seuils in styles.items()}
    mesures_champion, paquet_champion = mesurer_champion(evaluation, jours)

    champion_decisif = (mesures_champion or {}).get(STYLE_DE_DECISION)
    decision, raison = decider_promotion(champion_decisif, mesures_challenger[STYLE_DE_DECISION])
    for nom, mesures in (("champion", mesures_champion), ("challenger", mesures_challenger)):
        for style, m in (mesures or {}).items():
            log.info("%-10s %-12s %4s ordres | accuracy %s", nom, style, m.get("ordres"),
                     m.get("accuracy"))
    log.info("DECISION : %s (%s)", decision.upper(), raison)

    version = f"{date}"
    paquet = {
        "modele": modele,
        "colonnes": colonnes,
        "cible": "sens de la prochaine bougie",
        "profil": "day_trading",
        "styles": styles,
        "style_le_plus_prudent": "conservateur",
        "mesures": {s: {"seuils": styles[s], "mesures_evaluation": mesures_challenger[s]}
                    for s in styles},
        "accuracy_globale": float(((modele.predict_proba(evaluation[colonnes])[:, 1] >= 0.5)
                                   .astype(int) == evaluation["label"]).mean()),
        "entraine_le": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "extrait_sha256": manifeste["sha256"],
        "fenetre": [str(jeu["open_time"].min()), str(coupure)],
    }
    run_id = journaliser_mlflow(paquet, manifeste, mesures_champion, mesures_challenger,
                                decision, raison, len(apprentissage), len(evaluation),
                                paquet_champion)
    paquet["mlflow_run_id"] = run_id
    joblib.dump(paquet, dossier / "candidat.joblib")

    resume = {
        "date": date, "version": version, "decision": decision, "raison": raison,
        "mlflow_run_id": run_id,
        "evaluation": [str(evaluation["open_time"].min()), str(evaluation["open_time"].max())],
        "champion": {"version": paquet_champion.get("version", "initiale")
                     if paquet_champion else None, "mesures": mesures_champion},
        "challenger": {"version": version, "mesures": mesures_challenger},
    }
    (dossier / "decision.json").write_text(json.dumps(resume, indent=2, default=str),
                                          encoding="utf-8")
    enregistrer_duel(resume, manifeste)
    return resume


def inscrire_champion_initial(client, paquet_champion: dict) -> None:
    """Premier duel : le modele en service entre au registre, alias 'champion'.

    Il a ete entraine a l'etape 3, avant que le registre existe. Sans cette
    inscription, l'alias ne designerait rien jusqu'a la premiere promotion,
    et le registre mentirait sur ce qui tourne en production.
    """
    import mlflow

    try:
        client.get_model_version_by_alias(NOM_MODELE, "champion")
        return                                     # deja inscrit
    except Exception:
        pass
    if not paquet_champion.get("modele"):
        return
    with mlflow.start_run(run_name=f"champion_{paquet_champion.get('version', 'initiale')}") as run:
        mlflow.log_params({"version": paquet_champion.get("version", "initiale"),
                           "role": "modele en service au premier duel",
                           "entraine_le": paquet_champion.get("entraine_le"),
                           "extrait_sha256": paquet_champion.get("extrait_sha256")})
        mlflow.sklearn.log_model(paquet_champion["modele"], name="modele",
                                 registered_model_name=NOM_MODELE,
                                 skops_trusted_types=TYPES_SURS)
    version = client.search_model_versions(f"name='{NOM_MODELE}' and run_id='{run.info.run_id}'")[0]
    client.set_registered_model_alias(NOM_MODELE, "champion", version.version)
    log.info("MLflow : modele en service inscrit (version %s, alias 'champion')", version.version)


def journaliser_mlflow(paquet, manifeste, champion, challenger, decision, raison,
                       lignes_apprentissage, lignes_evaluation,
                       paquet_champion: dict | None = None) -> str | None:
    """Trace le challenger dans MLflow et l'inscrit au registre des modeles.

    Chaque challenger devient une VERSION du modele enregistre ; l'alias
    "champion" ne bouge qu'a la publication. Le registre raconte ainsi toute
    l'histoire : ce qui a ete essaye, et ce qui a servi.
    """
    import mlflow

    suivi = os.getenv("MLFLOW_TRACKING_URI",
                      f"sqlite:///{(config.ROOT / 'mlflow.db').as_posix()}")
    try:
        mlflow.set_tracking_uri(suivi)
        mlflow.set_experiment(EXPERIENCE)
        inscrire_champion_initial(mlflow.MlflowClient(), paquet_champion or {})
        with mlflow.start_run(run_name=f"challenger_{paquet['version']}") as run:
            mlflow.log_params({
                "version": paquet["version"], "fenetre_jours": FENETRE_JOURS,
                "evaluation_jours": EVALUATION_JOURS, "coupure": manifeste["coupure"],
                "extrait_sha256": manifeste["sha256"], "variables": len(paquet["colonnes"]),
                "lignes_apprentissage": lignes_apprentissage,
                "lignes_evaluation": lignes_evaluation,
                "modele": "RandomForestClassifier + calibration isotonique",
            })
            metriques = {}
            for nom, mesures in (("challenger", challenger), ("champion", champion)):
                for style, m in (mesures or {}).items():
                    if m.get("ordres"):
                        metriques[f"{nom}_{style}_accuracy"] = m["accuracy"]
                        metriques[f"{nom}_{style}_ordres"] = m["ordres"]
                        metriques[f"{nom}_{style}_gain_net_pct"] = m["rendement_moyen_net_pct"]
            mlflow.log_metrics(metriques)
            mlflow.set_tags({"decision": decision, "raison": raison})
            mlflow.sklearn.log_model(paquet["modele"], name="modele",
                                     registered_model_name=NOM_MODELE,
                                     skops_trusted_types=TYPES_SURS)
            return run.info.run_id
    except Exception as exc:
        # MLflow est un outil de TRACABILITE : son indisponibilite ne doit
        # pas bloquer la production. L'echec est journalise et visible.
        log.error("MLflow indisponible (%s : %s) : run non trace", type(exc).__name__, exc)
        return None


def enregistrer_duel(resume: dict, manifeste: dict) -> None:
    """Une ligne dans la table reentrainements, lue par Grafana."""
    from src.database import postgres_connection

    champion = (resume["champion"]["mesures"] or {}).get(STYLE_DE_DECISION) or {}
    challenger = resume["challenger"]["mesures"][STYLE_DE_DECISION]
    with postgres_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO reentrainements
                   (date_coupure, extrait_sha256, fenetre_jours, evaluation_debut,
                    evaluation_fin, champion_version, champion_accuracy, champion_ordres,
                    challenger_version, challenger_accuracy, challenger_ordres,
                    decision, raison, mlflow_run_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (manifeste["coupure"], manifeste["sha256"], FENETRE_JOURS,
             resume["evaluation"][0], resume["evaluation"][1],
             resume["champion"]["version"], champion.get("accuracy"), champion.get("ordres"),
             resume["challenger"]["version"], challenger.get("accuracy"),
             challenger.get("ordres"), resume["decision"], resume["raison"],
             resume["mlflow_run_id"]))


# ---------------------------------------------------------------------------
#  3. Publier
# ---------------------------------------------------------------------------

def remplacer_atomiquement(source: Path, cible: Path) -> None:
    """Copie a cote, puis renommage : jamais de fichier a moitie ecrit.

    L'API relit le modele des que sa date change. Si on ecrivait directement
    dans le fichier en service, elle pourrait lire 40 Mo sur 100 et planter.
    os.replace est atomique : l'API voit l'ancien fichier, puis le nouveau,
    jamais un melange.
    """
    temporaire = cible.with_name(f".{cible.name}.tmp")
    shutil.copyfile(source, temporaire)
    os.replace(temporaire, cible)


def publier(date: str) -> dict:
    import joblib

    from scripts.pistes_amelioration import construire, construire_colonnes_seules
    from scripts.reference_derive import colonnes_surveillees, construire_reference

    dossier = dossier_du(date)
    resume = json.loads((dossier / "decision.json").read_text(encoding="utf-8"))
    if resume["decision"] != "promu":
        log.info("Challenger %s rejete : rien a publier (%s)", date, resume["raison"])
        return resume

    # 1. L'ancien champion est archive, jamais efface : un retour en arriere
    #    reste possible en recopiant un fichier.
    ARCHIVES.mkdir(parents=True, exist_ok=True)
    if CHAMPION.exists():
        ancien = joblib.load(CHAMPION)
        nom = f"direction_{ancien.get('version', 'initiale')}.joblib"
        shutil.copyfile(CHAMPION, ARCHIVES / nom)
        log.info("Ancien champion archive : %s", ARCHIVES / nom)

    # 2. La reference de derive decrit desormais les donnees du NOUVEAU modele.
    manifeste = json.loads((dossier / "manifest.json").read_text(encoding="utf-8"))
    jeu = construire_colonnes_seules(construire(pd.read_parquet(dossier / "day_trading.parquet"),
                                                set()), set())
    coupure = pd.Timestamp(manifeste["coupure"])
    developpement = jeu[jeu["open_time"] <= coupure - pd.Timedelta(days=EVALUATION_JOURS) - PURGE]
    apprentissage = developpement.iloc[:int(len(developpement) * 0.72)]
    reference = construire_reference(apprentissage, colonnes_surveillees(jeu), manifeste["sha256"])
    temporaire = dossier / "reference_derive.json"
    temporaire.write_text(json.dumps(reference, indent=2), encoding="utf-8")

    # 3. Mise en service : le modele d'abord, la reference ensuite.
    remplacer_atomiquement(dossier / "candidat.joblib", CHAMPION)
    remplacer_atomiquement(temporaire, REFERENCE_DERIVE)
    log.info("Nouveau champion en service : version %s", resume["version"])

    deplacer_alias(resume.get("mlflow_run_id"))
    return resume


def deplacer_alias(run_id: str | None) -> None:
    """L'alias 'champion' du registre MLflow suit le modele en service."""
    if not run_id:
        return
    import mlflow

    try:
        mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI",
                                          f"sqlite:///{(config.ROOT / 'mlflow.db').as_posix()}"))
        client = mlflow.MlflowClient()
        versions = client.search_model_versions(f"name='{NOM_MODELE}' and run_id='{run_id}'")
        if versions:
            client.set_registered_model_alias(NOM_MODELE, "champion", versions[0].version)
            log.info("MLflow : alias 'champion' -> version %s", versions[0].version)
    except Exception as exc:
        log.error("MLflow : alias non deplace (%s : %s)", type(exc).__name__, exc)


def main():
    parser = argparse.ArgumentParser(description="Reentrainement champion / challenger")
    parser.add_argument("etape", choices=["figer", "entrainer", "publier"])
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                        help="Identifiant du reentrainement (date du lancement)")
    args = parser.parse_args()
    {"figer": figer, "entrainer": entrainer, "publier": publier}[args.etape](args.date)


if __name__ == "__main__":
    main()
