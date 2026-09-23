"""Cree (ou complete) le fichier .env a partir de .env.example.

Chaque valeur __A_GENERER__ devient un secret aleatoire, tire par le module
`secrets` de Python (source d'aleatoire cryptographique du systeme, pas
`random`, qui est previsible).

IDEMPOTENT : une valeur deja presente dans .env n'est JAMAIS remplacee.
Regenerer une cle d'API ou un mot de passe a chaque lancement casserait tout
ce qui l'utilise deja (base Airflow creee avec l'ancien mot de passe, par
exemple).

Usage :
    python -m scripts.generer_secrets
    python -m scripts.generer_secrets --afficher   # rappelle les identifiants
"""
from __future__ import annotations

import argparse
import base64
import secrets
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
MODELE = RACINE / ".env.example"
CIBLE = RACINE / ".env"

MARQUEUR = "__A_GENERER__"
MARQUEUR_FERNET = "__A_GENERER_FERNET__"


def generer(marqueur: str) -> str:
    if marqueur == MARQUEUR_FERNET:
        # Format impose par Airflow : 32 octets encodes en base64 URL.
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    # 32 octets = 64 caracteres hexadecimaux : bien au-dela du minimum de 32
    # exige par l'API (api/securite.py).
    return secrets.token_hex(32)


def lire(chemin: Path) -> dict[str, str]:
    valeurs = {}
    if chemin.exists():
        for ligne in chemin.read_text(encoding="utf-8").splitlines():
            if "=" in ligne and not ligne.lstrip().startswith("#"):
                cle, _, valeur = ligne.partition("=")
                valeurs[cle.strip()] = valeur.strip()
    return valeurs


def completer(modele: str, existantes: dict[str, str]) -> tuple[str, list[str]]:
    """Le contenu du nouveau .env, et la liste des secrets crees."""
    lignes, crees = [], []
    for ligne in modele.splitlines():
        if "=" not in ligne or ligne.lstrip().startswith("#"):
            lignes.append(ligne)
            continue
        cle, _, defaut = ligne.partition("=")
        cle, defaut = cle.strip(), defaut.strip()
        if cle in existantes and existantes[cle] not in (MARQUEUR, MARQUEUR_FERNET, ""):
            valeur = existantes[cle]
        elif defaut in (MARQUEUR, MARQUEUR_FERNET):
            valeur = generer(defaut)
            crees.append(cle)
        else:
            valeur = existantes.get(cle) or defaut
        lignes.append(f"{cle}={valeur}")

    # Variables ajoutees a la main dans .env et absentes du modele : gardees.
    connues = {l.partition("=")[0].strip() for l in modele.splitlines() if "=" in l}
    supplementaires = [f"{c}={v}" for c, v in existantes.items() if c not in connues]
    if supplementaires:
        lignes += ["", "# --- Ajouts locaux ---", *supplementaires]
    return "\n".join(lignes) + "\n", crees


def main():
    parser = argparse.ArgumentParser(description="Genere les secrets de .env")
    parser.add_argument("--afficher", action="store_true",
                        help="Affiche les identifiants des interfaces web")
    args = parser.parse_args()

    existantes = lire(CIBLE)
    contenu, crees = completer(MODELE.read_text(encoding="utf-8"), existantes)
    CIBLE.write_text(contenu, encoding="utf-8")

    if crees:
        print(f"{CIBLE.name} : {len(crees)} secret(s) cree(s) -> {', '.join(crees)}")
    else:
        print(f"{CIBLE.name} : deja complet, rien n'a ete modifie.")

    if args.afficher or crees:
        valeurs = lire(CIBLE)
        print("\nIdentifiants des interfaces web (a garder pour soi) :")
        print(f"  Airflow  http://localhost:{valeurs.get('AIRFLOW_PORT')}   "
              f"{valeurs.get('AIRFLOW_ADMIN_USER')} / {valeurs.get('AIRFLOW_ADMIN_PASSWORD')}")
        print(f"  Grafana  http://localhost:{valeurs.get('GRAFANA_PORT')}   "
              f"admin / {valeurs.get('GRAFANA_ADMIN_PASSWORD')}")
        print(f"  pgAdmin  http://localhost:{valeurs.get('PGADMIN_PORT')}   "
              f"{valeurs.get('PGADMIN_EMAIL')} / {valeurs.get('PGADMIN_PASSWORD')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
