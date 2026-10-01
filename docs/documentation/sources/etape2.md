---
titre: Organisation des données
sous_titre: Une couche brute dans MongoDB, une couche exploitable dans PostgreSQL, reliées par une clé métier
etape: 2
fichier: CryptoBot_etape2_bases
modele: fiche
---

## Ce que l'étape demandait

Choisir où stocker les données collectées, concevoir le modèle de données et écrire le chargement, en justifiant le choix des bases et la façon dont elles se relient.

## Nos choix et pourquoi

| Choix | Pourquoi | Alternative écartée |
|---|---|---|
| Répartir selon la forme de la donnée | une bougie a toujours 13 colonnes : relationnel ; les métadonnées de Binance ont des champs variables et imbriqués : documents | « SQL pour l'historique, NoSQL pour le temps réel », alors que la bougie est la même |
| PostgreSQL pour la couche exploitable | transactions, jointures, contraintes qui refusent une donnée incohérente ; 2 millions de lignes restent loin de ses limites | InfluxDB, ClickHouse : faits pour des milliards de lignes, sans transactions |
| Extension TimescaleDB | découpe les bougies en tranches de temps, compresse les anciennes ; fournie dans l'image Docker officielle | Snowflake, BigQuery : payants et hébergés, hors conteneurs |
| MongoDB pour la couche brute | garde les réponses de Binance telles quelles, pour rejouer un nettoyage corrigé | Elasticsearch, conçu pour la recherche |
| Clé métier (paire, pas de temps, ouverture) | identifie une bougie par ce qui la définit ; aucun doublon possible | un identifiant technique |
| Champ `raw_ref` sur chaque bougie | aucun moteur ne pose de clé étrangère vers l'autre ; on remonte ainsi au document brut d'origine | deux bases sans lien vérifiable |
| Une table par profil, lue par des vues | chaque pas de temps stocké une fois ; compression réglée par profil | une table unique |
| Chargement idempotent | une bougie présente est mise à jour, jamais dupliquée : relancer ne crée aucun doublon | une insertion simple |

Les variables calculées à l'étape 3 sont stockées en JSONB plutôt qu'en colonnes : leur nombre a changé plusieurs fois, et chaque essai aurait demandé une migration du schéma.

## Résultat

- 2 075 570 bougies chargées dans trois tables de profil, protégées par des contraintes (prix cohérents, volumes positifs, source connue).
- Quatre collections MongoDB, dont un carnet d'ordres effacé automatiquement après 30 jours.
- Le modèle a accueilli les besoins des étapes 3 à 5 sans refonte, par des fichiers SQL numérotés.

## Limite

Le lien entre les deux bases repose sur la clé métier et sur `raw_ref` : aucun moteur ne le vérifie, c'est au chargement de l'écrire correctement.
