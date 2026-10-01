---
titre: Déploiement
sous_titre: Une API sécurisée pour le modèle et les données, conteneurisée, testée, avec une mesure de la dérive
etape: 4
fichier: CryptoBot_etape4_deploiement
modele: fiche
---

## Ce que l'étape demandait

Exposer le modèle et les bases par une API, la tester, conteneuriser l'API et les bases de données, et mesurer la dérive des données.

## Nos choix et pourquoi

| Choix | Pourquoi | Alternative écartée |
|---|---|---|
| FastAPI et Pydantic | une requête mal formée est refusée avant le code (422) ; documentation interactive produite d'office | écrire la validation à la main |
| Clé d'API, une par client | nos clients sont des programmes, sans utilisateur : c'est l'usage des clés d'API ; une clé compromise se révoque seule | JWT, pensé pour des personnes qui se connectent avec un mot de passe |
| nginx injecte la clé de l'interface | tout ce qui arrive dans le navigateur peut être lu ; la page appelle `/api/...` et nginx ajoute la clé | mettre la clé dans la page |
| API sans port publié, conteneurs non-root | seuls nginx, Airflow et Prometheus la joignent ; un conteneur compromis n'a pas les droits administrateur | API ouverte sur la machine |
| Modèle monté en lecture seule, hors de l'image | un nouveau modèle se met en service sans reconstruire l'image, et l'API le recharge à chaud | modèle copié dans l'image |
| Tests avec doublures | fausses bougies, faux modèle : les tests tournent partout en quelques secondes et un échec désigne un bug du code | tests branchés sur les vraies bases |
| PSI par paire et par pas de temps, sur 90 jours | une variable n'a pas la même échelle sur le BTC et le XRP, en 15 min et en 4 h ; une référence mélangée signalait une dérive inexistante | une référence unique pour tout |

Le PSI (indice de stabilité de population) compare la répartition récente de chaque variable à celle de l'apprentissage : sous 0,10 elle est stable, au-delà de 0,25 la dérive est forte.

## Résultat

- 11 routes, 149 tests unitaires, et 21 vérifications de l'application démarrée (clé ajoutée par nginx, API invisible de l'extérieur, limite de 10 requêtes par seconde).
- Tout démarre en une commande avec Docker Compose ; les ports ne sont ouverts que sur `127.0.0.1`.
- Au 23 septembre 2026, les 15 séries surveillées dérivent, surtout sur la volatilité et la taille moyenne d'un trade.

## Limite

La dérive mesurée ne dit pas si le modèle se trompe davantage : elle signale que le marché a changé depuis l'apprentissage. L'étape 5 montre qu'elle ne s'accompagne pas forcément d'une baisse de performance.
