---
titre: Collecte des données
sous_titre: Un historique propre de 5 paires sur 7 pas de temps, et un flux temps réel au même format
etape: 1
fichier: CryptoBot_etape1_collecte
modele: fiche
---

## Ce que l'étape demandait

Choisir une source de données de marché, la comprendre et écrire une fonction de récupération générique (n'importe quelle paire, n'importe quel pas de temps), pour obtenir un jeu de données propre et documenté.

## Nos choix et pourquoi

| Choix | Pourquoi | Alternative écartée |
|---|---|---|
| API publique de Binance | données de marché gratuites, sans clé ni inscription, en REST et en WebSocket | une source payante ou à inscription |
| 5 paires contre USDT (BTC, ETH, BNB, SOL, XRP) | très échangées, donc des bougies régulières, sans trous ni prix aberrants | des paires peu actives, aux bougies irrégulières |
| Trois profils : scalping, day trading, swing | chaque stratégie lit le marché à son échelle, avec ses pas de temps et sa profondeur | un pas de temps unique |
| Pas de temps partagé collecté une seule fois | sur la plus longue profondeur, il sert les deux profils : 7 collectes au lieu de 9 | une collecte par profil, avec doublons |
| Historique commun depuis septembre 2020 | SOLUSDT n'existe que depuis août 2020 ; il faut comparer les paires sur la même période | remonter à 2017 pour BTC et ETH seulement |
| REST pour l'historique, WebSocket pour le direct | REST dit ce qui s'est passé, le WebSocket ce qui se passe ; seule la bougie clôturée est gardée | interroger REST en boucle |
| Pagination sur la dernière bougie reçue | 1 000 bougies au plus par appel ; un compteur sauterait des données si un lot est incomplet | avancer d'un nombre fixe d'intervalles |
| Un schéma pivot de 13 colonnes, dates en UTC | une seule forme pour la même bougie, quelle que soit sa provenance ; le brut reste archivé en JSON | traiter chaque format séparément |

## Résultat

- 35 jeux de données (5 paires, 7 pas de temps) : 2 075 570 bougies, 132 Mo en Parquet, collectés en une dizaine de minutes.
- Aucun doublon, aucune valeur nulle, aucune incohérence de prix ; complétude de 100 %.
- Paires, pas de temps et dates sont des paramètres : une nouvelle paire se collecte sans modifier le code.

## Limite

Les arrêts de Binance entre 2020 et 2023 laissent des trous en pas horaire (99,96 % de complétude) mais se fondent en 4 h dans une bougie au volume anormalement faible. Nous ne comblons pas ces trous par des prix inventés.
