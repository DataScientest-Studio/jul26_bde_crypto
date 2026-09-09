"""Coherence entre la configuration Python et le schema SQL.

Les profils sont declares a deux endroits : src/config.py, dont se sert le
collecteur, et sql/02_seed.sql, dont se sert la base. C'est un doublon
assume - la base doit etre comprehensible sans lire le code Python - mais
un doublon non surveille finit toujours par diverger.

Ces tests echouent des que les deux versions ne racontent plus la meme
histoire. Ils ne demandent aucune base demarree.
"""
import re
import sys
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

from src import config

SQL_DIR = RACINE / "sql"


def _valeurs_insert(fichier: str, table: str) -> list[tuple[str, ...]]:
    """Extrait les n-uplets d'un INSERT du fichier de seed.

    On lit le SQL comme du texte plutot que via une base : les tests
    doivent tourner en CI sans conteneur.
    """
    sql = (SQL_DIR / fichier).read_text(encoding="utf-8")
    # On isole le bloc INSERT de la table demandee, jusqu'au point-virgule.
    bloc = re.search(rf"INSERT INTO {table}\b.*?;", sql, re.S | re.I)
    assert bloc, f"Aucun INSERT INTO {table} dans {fichier}"

    corps = bloc.group(0)
    corps = re.sub(r"--[^\n]*", "", corps)  # retirer les commentaires

    lignes = []
    for brut in re.findall(r"\(([^()]*)\)", corps.split("VALUES", 1)[1]):
        lignes.append(tuple(_decouper(brut)))
    return lignes


def _decouper(ligne: str) -> list[str]:
    """Decoupe sur les virgules SITUEES HORS des chaines SQL.

    Les descriptions contiennent des virgules : un split naif les prendrait
    pour des separateurs de colonnes.
    """
    champs, courant, dans_chaine = [], [], False
    for caractere in ligne:
        if caractere == "'":
            dans_chaine = not dans_chaine
        elif caractere == "," and not dans_chaine:
            champs.append("".join(courant).strip())
            courant = []
            continue
        courant.append(caractere)
    champs.append("".join(courant).strip())
    return [c.strip().strip("'").strip() for c in champs]


# --- Profils ---------------------------------------------------------------

def test_les_memes_profils_des_deux_cotes():
    sql_profils = {ligne[0] for ligne in _valeurs_insert("02_seed.sql", "trading_profiles")}
    assert sql_profils == set(config.TRADING_PROFILES), (
        "Les profils de src/config.py et de sql/02_seed.sql different"
    )


def test_les_memes_profondeurs_d_historique():
    for ligne in _valeurs_insert("02_seed.sql", "trading_profiles"):
        profil, _label, _description, history = ligne[0], ligne[1], ligne[2], ligne[3]
        attendu = config.TRADING_PROFILES[profil]["history_days"]
        if history.upper() == "NULL":
            assert attendu is None, f"{profil} : NULL en SQL mais {attendu} en Python"
        else:
            assert int(history) == attendu, f"{profil} : {history} en SQL, {attendu} en Python"


# --- Pas de temps ----------------------------------------------------------

def test_les_memes_pas_de_temps():
    sql_intervalles = {ligne[0] for ligne in _valeurs_insert("02_seed.sql", "intervals")}
    python_intervalles = set(config.resolve_intervals(list(config.TRADING_PROFILES)))
    assert sql_intervalles == python_intervalles


def test_les_durees_sont_exactes():
    """Une duree fausse en base fausserait tout controle de completude."""
    from src.preprocessing import interval_to_timedelta

    for ligne in _valeurs_insert("02_seed.sql", "intervals"):
        intervalle, duree = ligne[0], int(ligne[1])
        attendu = int(interval_to_timedelta(intervalle).total_seconds())
        assert duree == attendu, f"{intervalle} : {duree}s en SQL, {attendu}s attendu"


def test_owner_table_applique_la_regle_de_profondeur():
    """owner_table doit designer le profil qui conserve le pas de temps le
    plus longtemps. C'est la regle qui garantit l'absence de doublon."""
    plus_longue = {}
    for nom, profil in config.TRADING_PROFILES.items():
        for intervalle in profil["intervals"]:
            depth = profil["history_days"]
            actuel = plus_longue.get(intervalle)
            if actuel is None:
                plus_longue[intervalle] = (nom, depth)
            else:
                _, depth_actuelle = actuel
                # None l'emporte : c'est "tout l'historique".
                if depth_actuelle is not None and (depth is None or depth > depth_actuelle):
                    plus_longue[intervalle] = (nom, depth)

    for ligne in _valeurs_insert("02_seed.sql", "intervals"):
        intervalle, table_sql = ligne[0], ligne[2]
        profil_attendu = plus_longue[intervalle][0]
        assert table_sql == f"candles_{profil_attendu}", (
            f"{intervalle} : le SQL le range dans {table_sql}, "
            f"mais {profil_attendu} le conserve plus longtemps"
        )


# --- Association profil / pas de temps -------------------------------------

def test_chaque_profil_declare_tous_ses_pas_de_temps():
    associations = _valeurs_insert("02_seed.sql", "profile_intervals")
    par_profil: dict[str, set[str]] = {}
    for profil, intervalle, *_ in associations:
        par_profil.setdefault(profil, set()).add(intervalle)

    for nom, profil in config.TRADING_PROFILES.items():
        assert par_profil[nom] == set(profil["intervals"]), (
            f"{nom} : le SQL declare {par_profil[nom]}, "
            f"Python declare {set(profil['intervals'])}"
        )


def test_chaque_pas_de_temps_a_exactement_un_proprietaire():
    """Deux proprietaires dupliqueraient la donnee, zero la perdrait."""
    proprietaires: dict[str, list[str]] = {}
    for profil, intervalle, is_owner, *_ in _valeurs_insert("02_seed.sql", "profile_intervals"):
        if is_owner.upper() == "TRUE":
            proprietaires.setdefault(intervalle, []).append(profil)

    for intervalle in config.resolve_intervals(list(config.TRADING_PROFILES)):
        detenteurs = proprietaires.get(intervalle, [])
        assert len(detenteurs) == 1, (
            f"{intervalle} : {len(detenteurs)} proprietaire(s) {detenteurs}, "
            f"il en faut exactement un"
        )


def test_is_owner_concorde_avec_owner_table():
    tables = {ligne[0]: ligne[2] for ligne in _valeurs_insert("02_seed.sql", "intervals")}
    for profil, intervalle, is_owner, *_ in _valeurs_insert("02_seed.sql", "profile_intervals"):
        proprietaire = tables[intervalle] == f"candles_{profil}"
        declare = is_owner.upper() == "TRUE"
        assert declare == proprietaire, (
            f"({profil}, {intervalle}) : is_owner={declare} mais "
            f"owner_table={tables[intervalle]}"
        )


# --- Fichiers SQL ----------------------------------------------------------

@pytest.mark.parametrize("fichier", ["01_schema.sql", "02_seed.sql",
                                     "03_views.sql", "04_policies.sql"])
def test_le_sql_est_syntaxiquement_valide(fichier):
    """Analyse avec la grammaire PostgreSQL officielle (libpg_query).

    Attrape les fautes de frappe sans demarrer de conteneur, ce qui rend
    le test utilisable en CI a l'etape 5.
    """
    pglast = pytest.importorskip("pglast", reason="pglast non installe")
    pglast.parse_sql((SQL_DIR / fichier).read_text(encoding="utf-8"))


def test_les_trois_tables_de_faits_existent():
    schema = (SQL_DIR / "01_schema.sql").read_text(encoding="utf-8")
    for profil in config.TRADING_PROFILES:
        assert f"candles_{profil}" in schema, f"Table candles_{profil} absente du schema"


def test_chaque_table_de_faits_est_une_hypertable():
    """Sans create_hypertable, TimescaleDB n'apporte rien : la table reste
    une table PostgreSQL ordinaire, sans decoupage ni compression."""
    schema = (SQL_DIR / "01_schema.sql").read_text(encoding="utf-8")
    for profil in config.TRADING_PROFILES:
        assert f"create_hypertable('candles_{profil}'" in schema, (
            f"candles_{profil} n'est pas convertie en hypertable"
        )


def test_chaque_profil_a_sa_vue():
    vues = (SQL_DIR / "03_views.sql").read_text(encoding="utf-8")
    for profil in config.TRADING_PROFILES:
        assert f"CREATE VIEW v_{profil}" in vues, f"Vue v_{profil} manquante"
