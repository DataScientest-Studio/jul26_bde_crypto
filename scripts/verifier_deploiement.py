"""Verifie un deploiement qui tourne, de l'exterieur, comme un utilisateur.

Les tests unitaires verifient le code. Ce script verifie ce que le code
devient une fois assemble dans Docker : la page s'affiche-t-elle, la cle
est-elle bien injectee par le proxy, l'API est-elle vraiment inaccessible
directement, les en-tetes de securite sont-ils la ?

La CI le lance sur une pile demarree de zero ; on peut le lancer a la main
apres un `docker compose up -d`. Bibliotheque standard uniquement : il doit
tourner sur n'importe quelle machine, sans environnement Python prepare.

Usage :
    python scripts/verifier_deploiement.py
    python scripts/verifier_deploiement.py --profils pipeline supervision
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

RESULTATS: list[tuple[bool, str]] = []


def verifier(condition: bool, message: str) -> bool:
    RESULTATS.append((condition, message))
    print(f"  {'OK   ' if condition else 'ECHEC'}  {message}")
    return condition


def lire(url: str, entetes: dict | None = None, delai: float = 30) -> tuple[int, dict, str]:
    requete = urllib.request.Request(url, headers=entetes or {})
    try:
        with urllib.request.urlopen(requete, timeout=delai) as reponse:
            return reponse.status, dict(reponse.headers), reponse.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as erreur:
        return erreur.code, dict(erreur.headers), erreur.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as erreur:
        return 0, {}, str(erreur)


def port_ouvert(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def interface(base: str) -> None:
    print("\nInterface et proxy")
    statut, entetes, page = lire(f"{base}/")
    verifier(statut == 200 and "CryptoBot" in page, f"la page s'affiche ({statut})")
    entetes = {k.lower(): v for k, v in entetes.items()}
    verifier("default-src 'self'" in entetes.get("content-security-policy", ""),
             "politique de contenu (CSP) presente")
    verifier(entetes.get("x-frame-options") == "DENY", "affichage dans un cadre interdit")
    verifier(entetes.get("x-content-type-options") == "nosniff", "types de fichiers non devines")
    verifier(entetes.get("server", "") == "nginx", "version de nginx masquee")
    verifier("X-API-Key" not in page, "aucune cle d'API dans la page")

    statut, _, _ = lire(f"{base}/static/lightweight-charts.js")
    verifier(statut == 200, "bibliotheque de graphiques servie localement")

    statut, _, corps = lire(f"{base}/api/health")
    verifier(statut == 200 and '"statut"' in corps, f"API joignable a travers le proxy ({statut})")

    # 503 est acceptable (pas de modele dans la CI) ; 401 prouverait que la
    # cle n'est PAS injectee par nginx.
    statut, _, _ = lire(f"{base}/api/modele")
    verifier(statut in (200, 503), f"cle injectee par le proxy (/api/modele -> {statut}, pas 401)")

    statut, _, _ = lire(f"{base}/api/metrics")
    verifier(statut == 404, f"metriques invisibles depuis l'exterieur ({statut})")

    statut, _, _ = lire(f"{base}/api/graphique/DOGEUSDT?interval=1h")
    verifier(statut == 404, f"paire hors liste blanche refusee ({statut})")

    statut, _, _ = lire(f"{base}/api/docs")
    verifier(statut == 200, "documentation interactive disponible sur /api/docs")


def limitation_de_debit(base: str) -> None:
    print("\nLimitation de debit")
    with ThreadPoolExecutor(max_workers=20) as groupe:
        statuts = list(groupe.map(lambda _: lire(f"{base}/api/health", delai=10)[0], range(120)))
    refuses = statuts.count(429)
    verifier(refuses > 0, f"rafale de 120 appels : {refuses} refuses en 429 par nginx")


def isolement_de_l_api() -> None:
    print("\nIsolement de l'API")
    verifier(not port_ouvert(8000), "l'API n'est pas publiee sur la machine (port 8000 ferme)")
    # Depuis l'interieur du reseau Docker, sans cle : l'API doit refuser.
    code = ("import urllib.request, urllib.error\n"
            "try:\n urllib.request.urlopen('http://localhost:8000/modele'); print(200)\n"
            "except urllib.error.HTTPError as e: print(e.code)")
    try:
        sortie = subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-c", code],
                                capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as erreur:
        sortie = str(erreur)
    verifier(sortie == "401", f"appel direct sans cle refuse par l'API ({sortie})")


def supervision() -> None:
    print("\nSupervision")
    statut, _, corps = lire("http://127.0.0.1:9090/api/v1/targets")
    if verifier(statut == 200, "Prometheus repond"):
        cibles = json.loads(corps)["data"]["activeTargets"]
        en_panne = [c["labels"].get("service", c["labels"]["job"])
                    for c in cibles if c["health"] != "up"]
        verifier(not en_panne, f"{len(cibles)} cibles lues, en echec : {en_panne or 'aucune'}")
    statut, _, corps = lire("http://127.0.0.1:9090/api/v1/rules")
    if statut == 200:
        regles = sum(len(g["rules"]) for g in json.loads(corps)["data"]["groups"])
        verifier(regles > 0, f"{regles} regles d'alerte chargees")
    statut, _, _ = lire("http://127.0.0.1:3000/api/health")
    verifier(statut == 200, "Grafana repond")


def pipeline() -> None:
    print("\nPipeline")
    statut, _, corps = lire("http://127.0.0.1:8088/api/v2/monitor/health")
    verifier(statut == 200 and '"healthy"' in corps, f"Airflow repond et se dit sain ({statut})")
    statut, _, _ = lire("http://127.0.0.1:5000/health")
    verifier(statut == 200, "MLflow repond")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verification d'un deploiement CryptoBot")
    parser.add_argument("--interface", default="http://127.0.0.1:8080")
    parser.add_argument("--profils", nargs="*", default=[],
                        choices=["pipeline", "supervision"])
    args = parser.parse_args()

    interface(args.interface)
    isolement_de_l_api()
    if "supervision" in args.profils:
        supervision()
    if "pipeline" in args.profils:
        pipeline()
    # En dernier : la rafale fait monter les compteurs de refus.
    limitation_de_debit(args.interface)

    echecs = [message for ok, message in RESULTATS if not ok]
    print(f"\n{len(RESULTATS) - len(echecs)}/{len(RESULTATS)} verifications reussies")
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
