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
docker compose up -d              # TimescaleDB + MongoDB
python -m scripts.load_to_db --check   # vérifier les connexions
python -m scripts.load_to_db           # charger les 35 jeux de données
python -m scripts.check_db             # état des deux bases
```

Les scripts de `sql/` et `mongo/` sont joués automatiquement à la création
des conteneurs. Pour les rejouer après modification : `docker compose down -v`.

| Fichier | Rôle |
|---|---|
| `docker-compose.yml` | Les deux bases, avec healthchecks et volumes nommés |
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
| 2 | Organisation des données (SQL + NoSQL, UML) | 4 septembre | ⏳ |
| 3 | Consommation — modèle de ML | 11 septembre | — |
| 4 | Déploiement — API, Docker, dérive | 21 septembre | — |
| 5 | Automatisation & monitoring — CI, Airflow | soutenance | — |
| 6 | Soutenance | semaine du 5 octobre | — |
