# CryptoBot — jul26_bde_crypto

Bot de trading crypto piloté par Machine Learning.
Projet fil rouge — cursus Data Engineer.

## Étape 1 — Découverte des sources de données ✅

Collecte de données de marché Binance sur 5 paires majeures, via API REST
(historique) et WebSocket (temps réel).

**Livrables** :
- [`docs/rapport_etape1.md`](docs/rapport_etape1.md) — rapport explicatif complet
- [`samples/exemple_donnees_binance.json`](samples/exemple_donnees_binance.json) — exemple de données commenté
- [`docs/rapport_qualite.json`](docs/rapport_qualite.json) — contrôle qualité automatisé

**Résultats** : 262 415 bougies horaires sur 6 ans (2020-09 → 2026-08),
complétude 99,96 %, 0 doublon, 0 valeur nulle, 0 incohérence OHLC.

## Installation

```bash
pip install -r requirements.txt
```

Aucune clé API n'est nécessaire : le projet n'utilise que les endpoints
publics de données de marché.

## Utilisation

```bash
# Historique complet (paires et intervalle définis dans src/config.py)
python -m scripts.collect_history

# À la demande — le code est générique, rien n'est codé en dur
python -m scripts.collect_history --paires ADAUSDT DOGEUSDT --intervalle 4h
python -m scripts.collect_history --debut 2024-01-01

# Temps réel (WebSocket), 1 heure
python -m scripts.collect_stream --duree 3600

# Régénérer le fichier d'exemple du livrable
python -m scripts.make_samples

# Tests
python -m pytest tests/ -v
```

## Architecture

```
REST /api/v3/klines  ──┐
   (le passé)          │
                       ├──> preprocessing.py ──> data/raw     (JSON brut)
                       │    SCHÉMA PIVOT           data/processed (Parquet)
WebSocket @kline_1m  ──┘                                │
   (le présent)                                         ▼
                                                  ÉTAPE 2 : bases de données
```

Les deux sources arrivent dans des formats incompatibles (tableau positionnel
vs objet à clés d'une lettre) et convergent vers **un schéma unique**. Tout ce
qui est en aval ignore la provenance de la donnée.

## Organisation

| Chemin | Rôle |
|---|---|
| `src/config.py` | Paires, intervalles, endpoints, quotas — **seul endroit à modifier** |
| `src/binance_rest.py` | Client REST : pagination, gestion du quota, bascule sur miroir |
| `src/binance_ws.py` | Collecteur WebSocket : filtre `x=true`, reconnexion automatique |
| `src/preprocessing.py` | Normalisation vers le schéma pivot + contrôle qualité |
| `scripts/` | Points d'entrée exécutables |
| `data/raw/` | Réponses API intactes (rejouables) |
| `data/processed/` | Parquet normalisé |

## Feuille de route

| Étape | Objet | Échéance | État |
|---|---|---|---|
| 1 | Découverte des sources de données | 24 août | ✅ |
| 2 | Organisation des données (SQL + NoSQL, UML) | 4 septembre | ⏳ |
| 3 | Consommation — modèle de ML | 11 septembre | — |
| 4 | Déploiement — API, Docker, dérive | 21 septembre | — |
| 5 | Automatisation & monitoring — CI, Airflow | soutenance | — |
| 6 | Soutenance | semaine du 5 octobre | — |
