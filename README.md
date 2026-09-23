# CryptoBot — jul26_bde_crypto

Bot de trading crypto piloté par Machine Learning.
Projet fil rouge — cursus Data Engineer.

## Étape 1 — Récupération des données ✅

Collecte de données de marché Binance sur 5 paires majeures, via API REST
(historique) et WebSocket (temps réel), pour trois profils de trading.

**Livrables** :
- [`docs/rapport_etape1_v2.pdf`](docs/rapport_etape1_v2.pdf) — rapport explicatif (version courante)
- [`samples/exemple_donnees_binance.json`](samples/exemple_donnees_binance.json) — exemple de données collectées
- [`docs/rapport_qualite.json`](docs/rapport_qualite.json) — contrôle qualité des 35 jeux de données

**Historique des rapports** — les versions précédentes sont conservées pour
garder trace de l'évolution du projet :

| Version | Date | Périmètre | Fichier |
|---|---|---|---|
| v1 | 28 août 2026 | 1 pas de temps (1h), 262 415 lignes | [`rapport_etape1_v1.pdf`](docs/rapport_etape1_v1.pdf) |
| **v2** | 29 août 2026 | 7 pas de temps, 3 profils, 2 075 570 lignes | [`rapport_etape1_v2.pdf`](docs/rapport_etape1_v2.pdf) |

## Étape 2 — Organisation des données ✅

Choix des bases de données et modèle de données.

**Livrable** : [`docs/architecture_etape2.pdf`](docs/architecture_etape2.pdf)

### Démarrer les bases

```bash
python -m scripts.generer_secrets # une fois : crée .env (voir étape 4)
docker compose up -d              # toute l'application, dont les deux bases
python -m scripts.load_to_db --check   # vérifier les connexions
python -m scripts.load_to_db           # charger les 35 jeux de données
python -m scripts.check_db             # état des deux bases
```

Les scripts de `sql/` et `mongo/` sont joués automatiquement à la création
des conteneurs. Pour les rejouer après modification : `docker compose down -v`.

### Regarder les données dans le navigateur

`docker compose up -d` démarre aussi deux interfaces web (profil `outils`),
pratiques pour explorer les bases ou faire une capture d'écran :

| Interface | Adresse | Contenu |
|---|---|---|
| pgAdmin | <http://localhost:5050> | TimescaleDB : `candles_*`, les vues par profil, les tables de référence |
| mongo-express | <http://localhost:8081> | MongoDB : `raw_klines`, `exchange_info`, la couche brute |

Le serveur PostgreSQL est déjà enregistré dans pgAdmin
(`docker/pgadmin/servers.json`) : il suffit de le déplier et de saisir le mot
de passe `cryptobot` à la première connexion.

Ces deux interfaces ne servent **qu'à regarder** : aucun script du projet n'en
dépend. Elles n'écoutent que sur cette machine (`127.0.0.1`), jamais sur le
réseau.

| Fichier | Rôle |
|---|---|
| `docker-compose.yml` | Les deux bases, avec healthchecks et volumes nommés, plus les deux interfaces web |
| `docker/pgadmin/servers.json` | Connexion PostgreSQL pré-enregistrée dans pgAdmin |
| `sql/01_schema.sql` | Tables, contraintes, hypertables TimescaleDB, index |
| `sql/02_seed.sql` | Profils, pas de temps, règle de propriété |
| `sql/03_views.sql` | `v_scalping`, `v_day_trading`, `v_swing`, `v_coverage` |
| `sql/04_policies.sql` | Compression (active) et rétention (commentée) |
| `mongo/01_init.js` | Les 4 collections brutes et leurs index |
| `scripts/load_to_db.py` | Pipeline d'ingestion Parquet → PostgreSQL, JSON → MongoDB |
| `scripts/check_db.py` | État des bases : volumes, intégrité, retard de collecte |

**Note sur la couche brute** : un document MongoDB contient **1 000 bougies**,
soit exactement une réponse de l'API Binance. Ce n'est pas un compromis
technique mais la structure réelle de la donnée — et c'est nécessaire, un
document est plafonné à 16 Mo quand nos fichiers 1m en font 45.

| Base | Rôle | Ce qu'elle stocke |
|---|---|---|
| **PostgreSQL** | Couche exploitable | Bougies nettoyées (13 colonnes fixes), référentiel des paires, journal des collectes |
| **MongoDB** | Couche brute | Réponses API intactes, `exchangeInfo` et ses filtres polymorphes, messages WebSocket de tous types |

Le critère de répartition n'est pas la provenance mais **la forme de la donnée** :
structure stable → relationnel, structure variable → document. Les deux bases sont
reliées par la clé métier `symbol + interval + open_time`.

Une table de faits par profil de trading. Chaque pas de temps est stocké une seule
fois, dans la table du profil qui le conserve le plus longtemps — mais **chaque profil
utilise bien ses trois pas de temps**, le troisième étant lu par une vue :

| Profil | Pas de temps utilisés | Stockés dans sa table | Lus par vue | Lignes stockées |
|---|---|---|---|---:|
| `scalping` | 1m, 5m, **15m** | 1m, 5m | 15m ← `day_trading` | 1 559 135 |
| `day_trading` | 15m, 1h, **4h** | 15m, 1h | 4h ← `swing` | 438 275 |
| `swing` | 4h, 1d, 1w | 4h, 1d, 1w | — | 78 160 |

## Étape 3 — Machine learning ✅

**Livrables** :
- [`notebooks/etape3_modelisation.ipynb`](notebooks/etape3_modelisation.ipynb) —
  le notebook complet, exécuté, lisible directement sur GitHub : vérification de
  l'extrait figé, EDA, étiquetage, variables `ta`, comparaison des modèles,
  métriques (accuracy, bon sens directionnel, R²), GridSearchCV, backtest,
  MLflow et export `.joblib`
- [`docs/rapport_etape3.pdf`](docs/rapport_etape3.pdf) — le rapport de synthèse

Étiquetage par trois barrières (stop loss / take profit intégrés), 26 variables
sans échelle, 6 modèles comparés par profil avec deux références, GridSearchCV
en découpage chronologique, backtest en euros, suivi MLflow et export `.joblib`.

**Résultat** : la volatilité se prédit, la direction non. Le bon sens
directionnel reste proche de 50 % et les trois profils perdent au backtest une
fois les frais de 0,2 % pris en compte.

```bash
python -m scripts.make_extract --verify   # l'extrait fige est-il intact ?
python -m scripts.compare_models          # comparaison des modeles
python -m scripts.optimize_model          # GridSearchCV
python -m scripts.train_final             # entrainement final, MLflow, .joblib
python -m scripts.backtest_models         # backtest
mlflow ui --backend-store-uri sqlite:///mlflow.db

# ré-exécuter le notebook de bout en bout (quelques minutes)
python -m nbconvert --to notebook --execute --inplace notebooks/etape3_modelisation.ipynb
```

## Suite : nouvelle cible et modèle final (17 septembre)

Après la présentation de l'étape 3, la cible est devenue **le sens de la
prochaine bougie**, sur le profil day trading, avec un bouton
conservateur / agressif pour le produit final.

**Livrables** : section 11 du notebook, `models/direction_day_trading.joblib`,
expérience MLflow `cryptobot_direction`.

| Style | Achète si p ≥ | Vend si p ≤ | Ordres/jour | Bonnes réponses | Backtest |
|---|---|---|---|---|---|
| Agressif | 0,5346 | 0,4315 | 25 | 0,597 | −5,0 % |
| Conservateur | 0,5415 | 0,3921 | 20 | 0,599 | −3,6 % |

Un seuil **par côté** : les probabilités du modèle ne sont pas symétriques, et
un seuil unique rendait l'achat structurellement impossible en conservateur.

Mesures faites sur 14 831 bougies **postérieures à l'extrait figé**, jamais vues
par le modèle (24 août → 16 septembre).

**Résultat** : le modèle garde un avantage réel mais faible (environ 57 %), et il
n'est pas rentable. Sur une bougie de 15 minutes, le prix bouge en moyenne de
0,227 % contre 0,2 % de frais par aller-retour : même un modèle parfait ne
gagnerait que 0,027 % par ordre.

```bash
python -m scripts.direction_prochaine_bougie --contexte   # cible, sélectivité
python -m scripts.optimize_direction --activite 0.02 --plafond 0
python -m scripts.profils_de_risque          # seuils du bouton
python -m scripts.pistes_amelioration        # calibration, calendrier, BTC
python -m scripts.collect_order_book         # carnet d'ordres (futures)
python -m scripts.horizon_long               # horizons 4 h et 1 jour
python -m scripts.nouvelles_bougies          # bougies jamais vues
python -m scripts.backtest_direction         # backtest en euros
python -m scripts.train_direction_final      # modèle final, MLflow, .joblib
python scripts/make_notebook.py              # régénérer le notebook
```

## Étape 4 — Déploiement ✅

API du modèle et des bases, sécurisée, conteneurisée avec son interface,
testée, avec mesure de la dérive des données.

**Livrable** : [`docs/rapport_etape4.pdf`](docs/rapport_etape4.pdf)

### Lancer l'ensemble

```bash
python -m scripts.generer_secrets   # une fois : crée .env avec des secrets aléatoires
docker compose up -d --build        # toute l'application (16 services)
python scripts/verifier_deploiement.py --profils pipeline supervision
```

| Service | Adresse | Rôle |
|---|---|---|
| Interface | <http://localhost:8080> | le terminal : bougies en direct, décisions, carnet de trades |
| Documentation de l'API | <http://localhost:8080/api/docs> | chaque route, essayable depuis le navigateur |
| Airflow | <http://localhost:8088> | les trois DAG de l'étape 5 |
| MLflow | <http://localhost:5000> | expériences et registre des modèles |
| Grafana | <http://localhost:3000> | le tableau de bord de production |
| Prometheus | <http://localhost:9090> | métriques brutes, règles d'alerte |

Les identifiants d'Airflow et de Grafana sont dans `.env` ;
`python -m scripts.generer_secrets --afficher` les rappelle.

Les services sont rangés en **profils** (`COMPOSE_PROFILES` dans `.env`) :
le produit seul (bases, API, interface) démarre toujours ; `pipeline`,
`supervision` et `outils` s'ajoutent.

| Route | Ce qu'elle fait |
|---|---|
| `GET /health` | l'API, le modèle et les deux bases répondent-ils ? |
| `GET /modele` | d'où vient le modèle, ce qu'il vaut, ses deux seuils |
| `GET /paires` | les paires disponibles en base |
| `GET /bougies/{paire}` | les dernières bougies (lecture de PostgreSQL) |
| `GET /couverture` | volume et retard de collecte par jeu de données |
| `POST /prediction` | acheter / vendre / attendre, selon le style choisi |
| `GET /derive` | les données récentes ressemblent-elles à celles de l'entraînement ? |
| `GET /graphique/{paire}` | bougies + décision du modèle pour chacune (l'interface) |
| `GET /ordre/{paire}` | l'ordre projeté : entrée, take profit, stop loss |
| `POST /positions/{paire}` | un tour du carnet de positions virtuelles |
| `GET /metrics` | métriques Prometheus (réseau Docker interne seulement) |

Le **style** est le bouton conservateur / agressif : il fixe la probabilité
minimale à partir de laquelle le bot agit. En dessous, il répond `attendre`.

### Sécurité

| Mesure | Où |
|---|---|
| Une clé d'API **par client** (interface, Airflow), comparée en temps constant | `api/securite.py` |
| API **fermée par défaut** : sans clé configurée, 503 plutôt que s'ouvrir | `api/securite.py` |
| La clé de l'interface est ajoutée par le **proxy nginx**, jamais envoyée au navigateur | `interface/snippets/proxy_api.conf` |
| L'API n'est **pas publiée** sur la machine : seuls nginx, Airflow et Prometheus la joignent | `docker-compose.yml` |
| Réseaux Docker séparés : l'interface ne peut pas atteindre les bases | `docker-compose.yml` |
| Paires, pas de temps et styles validés par **liste blanche** avant tout appel à Binance | `api/main.py` |
| **Limitation de débit** (10 req/s par IP) et en-têtes de sécurité (CSP, anti-cadre) | `interface/nginx.conf.template` |
| Conteneurs **sans droits root** (API, interface) | `Dockerfile.api`, `interface/Dockerfile` |
| Secrets générés aléatoirement, jamais versionnés ; ports limités à `127.0.0.1` | `scripts/generer_secrets.py` |

Pourquoi des clés plutôt que des jetons JWT : nos clients sont des
**services**, pas des personnes qui se connectent avec un mot de passe.

Pour lancer l'API seule, sans Docker ni clé, en développement :

```bash
CRYPTOBOT_ACCES_LIBRE=1 uvicorn api.main:app --reload
```

### Tests

```bash
pytest tests/ -q                               # 149 tests, une dizaine de secondes
python scripts/verifier_deploiement.py         # 21 vérifications sur la pile qui tourne
```

Les tests n'ont besoin ni des bases ni du modèle de 100 Mo : les accès
extérieurs sont remplacés par des doublures, pour qu'un échec désigne un bug
du code et non une base éteinte. Ce qui ne se voit qu'une fois assemblé
(clé injectée par nginx, API invisible de l'extérieur, limitation de débit)
est vérifié par `scripts/verifier_deploiement.py`, sur la pile démarrée.

### Dérive des données

```bash
python -m scripts.reference_derive          # photographie des données d'entraînement
python -m scripts.mesurer_derive            # mesure du jour, archivée dans docs/
```

La mesure utilise l'indice PSI : pour chaque variable, on compare la
répartition actuelle à celle de l'entraînement. En dessous de 0,10 c'est
stable, au-delà de 0,25 un réentraînement est conseillé. La comparaison se
fait **par paire et par pas de temps**, sur **la même durée** pour chaque pas
de temps (90 jours), sans quoi on mesure des différences d'unités ou de
période plutôt qu'une dérive.

## Étape 5 — Automatisation et supervision ✅

L'application tourne seule, en continu, et se surveille.

### Airflow : trois DAG

| DAG | Quand | Ce qu'il fait |
|---|---|---|
| `cryptobot_collecte` | toutes les 15 min (à :01, :16, :31, :46) | bougies Binance → MongoDB → PostgreSQL, contrôle qualité, variables techniques, **un tour du bot** pour chaque paire, pas de temps et style |
| `cryptobot_derive` | chaque jour à 6 h 30 UTC | dérive par paire et pas de temps ; déclenche le réentraînement si 8 séries sur 15 dérivent fortement |
| `cryptobot_reentrainement` | chaque dimanche à 3 h UTC (ou sur dérive) | extrait figé et signé → nouveau modèle → **duel** contre le modèle en service sur 21 jours jamais vus → publication seulement s'il fait au moins aussi bien |

Airflow **orchestre**, il ne calcule pas : chaque tâche lance une commande du
projet (`python -m scripts.xxx`) dans son propre environnement Python. Ce sont
les mêmes commandes qu'à la main :

```bash
python -m scripts.ingestion_continue              # bougies manquantes, en base
python -m scripts.controle_qualite                # fraîcheur, trous, incohérences
python -m scripts.load_features --contexte --jours 2
python -m scripts.mesurer_derive --enregistrer
python -m scripts.reentrainer figer|entrainer|publier --date 2026-09-27
```

Le modèle promu remplace l'ancien en une opération atomique ; l'API le
recharge d'elle-même, sans redémarrer. L'ancien est archivé dans
`models/archives/`, et le registre MLflow déplace l'alias `champion`.

### Intégration continue (GitHub Actions)

À chaque envoi : analyse statique (ruff), tests (pytest + couverture),
chargement des DAG, validation des règles Prometheus et de nginx, puis
**déploiement de bout en bout** vérifié de l'extérieur. Si tout est vert, les
images `api`, `interface` et `airflow` sont publiées sur `ghcr.io`. Mettre à
jour une machine : `docker compose pull && docker compose up -d`.

### Supervision

| Outil | Rôle |
|---|---|
| Prometheus | lit les métriques de l'API et de l'exportateur, sonde chaque page web, évalue **12 règles d'alerte** (`monitoring/prometheus/alertes.yml`) |
| Exportateur | traduit l'état du pipeline en métriques : retard des données, dérive, âge du dernier tour du bot, issue du dernier duel |
| Grafana | tableau « CryptoBot – production », provisionné depuis `monitoring/grafana/` |

## Profils de trading

Un bot ne regarde pas le marché à la même échelle selon la stratégie visée.
Chaque profil définit ses intervalles **et** sa profondeur d'historique.

| Profil | Intervalles | Historique | Pourquoi cette profondeur |
|---|---|---|---|
| `scalping` | 1m, 5m, 15m | 180 jours | Au-delà de quelques mois, la microstructure du marché a trop changé pour rester pertinente à cette échelle |
| `day_trading` | 15m, 1h, 4h | 730 jours | Deux ans couvrent plusieurs régimes de marché sans noyer le modèle sous le bruit |
| `swing` | 4h, 1d, 1w | tout (depuis sept. 2020) | Une bougie hebdomadaire ne produit que ~50 lignes par an : il faut tout l'historique |

Quand deux profils réclament le même intervalle avec des profondeurs
différentes — `15m` en scalping (180 j) et en day trading (730 j) — la
**profondeur la plus longue l'emporte**. Collecter large satisfait les deux
profils ; l'inverse laisserait le second à court de données.

## Installation

```bash
pip install -r requirements.txt
```

Aucune clé API n'est nécessaire : le projet n'utilise que les endpoints
publics de données de marché.

## Utilisation

```bash
# Décrire les profils disponibles
python -m scripts.collect_history --list

# Collecter selon un profil
python -m scripts.collect_history --profile day_trading
python -m scripts.collect_history --profile scalping swing
python -m scripts.collect_history --profile all

# Collecte ponctuelle, hors profil
python -m scripts.collect_history --pairs ADAUSDT --intervals 4h --start 2024-01-01

# Temps réel (WebSocket)
python -m scripts.collect_stream --duration 3600
python -m scripts.collect_stream --stream kline_5m --duration 600

# Régénérer le fichier d'exemple du livrable
python -m scripts.make_samples

# Tests
python -m pytest tests/ -v
```

## Architecture

```
API REST  ──┐
(le passé)  │
            ├──> preprocessing.py ──> data/raw       (JSON brut)
            │    SCHÉMA PIVOT         data/processed (Parquet)
WebSocket ──┘                                │
(le présent)                                 ▼
                                     ÉTAPE 2 : bases de données
```

Les deux sources arrivent dans des formats incompatibles — tableau positionnel
côté REST, objet à clés d'une lettre côté WebSocket — et convergent vers **un
schéma unique**. Tout ce qui est en aval ignore la provenance de la donnée.

## Organisation

| Chemin | Rôle |
|---|---|
| `src/config.py` | Paires, profils, endpoints, quotas — **seul endroit à modifier** |
| `src/binance_rest.py` | Client REST : pagination, gestion du quota, bascule sur miroir |
| `src/binance_ws.py` | Collecteur WebSocket : filtre `x=true`, reconnexion automatique |
| `src/preprocessing.py` | Normalisation vers le schéma pivot, contrôle qualité |
| `scripts/` | Points d'entrée exécutables |
| `data/raw/` | Réponses API intactes (rejouables, non versionnées) |
| `data/processed/` | Parquet normalisé (non versionné) |

Les données collectées ne sont pas versionnées : elles se régénèrent avec
`python -m scripts.collect_history`.

## Convention de code

**Le code est en anglais, les commentaires et la documentation en français.**

Noms de variables, de fonctions, de colonnes et de fichiers en anglais — c'est
la convention universelle en Python et ça évite les mélanges du type
`normaliser_klines()`. Les commentaires, docstrings et messages de log restent
en français, parce qu'ils servent à expliquer nos choix à l'équipe et au jury.

```python
def normalize_klines(raw: list[list], symbol: str, interval: str) -> pd.DataFrame:
    """Transforme la reponse brute de /api/v3/klines en DataFrame exploitable.

    Binance envoie les prix en CHAINES de caracteres pour ne pas perdre de
    precision en JSON. Les laisser ainsi ferait echouer tout calcul.
    """
```

## Feuille de route

| Étape | Objet | Échéance | État |
|---|---|---|---|
| 1 | Récupération des données | 24 août | ✅ |
| 2 | Organisation des données (SQL + NoSQL, UML) | 4 septembre | ✅ |
| 3 | Consommation — modèle de ML | 11 septembre | ✅ |
| 4 | Déploiement — API, Docker, dérive | 21 septembre | ✅ |
| 5 | Automatisation & monitoring — CI, Airflow | soutenance | ✅ |
| 6 | Soutenance | semaine du 5 octobre | — |
