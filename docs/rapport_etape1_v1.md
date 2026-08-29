# Étape 1 — Récupération des données Binance

**Projet CryptoBot** — cursus Data Engineer
**Dépôt** : `DataScientest-Studio/jul26_bde_crypto`
**Date** : 28 août 2026

Quelles sources nous utilisons, comment nous récupérons les données, et à quoi
elles ressemblent une fois nettoyées.

| | |
|---|---|
| Sources utilisées | 2 (API REST + WebSocket) |
| Paires suivies | 5 |
| Bougies collectées | 262 415 |
| Données complètes | 99,96 % |

---

## 1. Ce que nous collectons

Nous récupérons l'historique des cours de cinq crypto-monnaies sur Binance,
sous forme de **bougies horaires**. Une bougie résume une heure de marché en
quelques chiffres : prix d'ouverture, plus haut, plus bas, prix de clôture, et
volume échangé.

| Choix | Valeur | Pourquoi |
|---|---|---|
| Plateforme | Binance | Premier exchange mondial, API publique gratuite, aucune inscription nécessaire pour les données de marché |
| Paires | BTC, ETH, BNB, SOL, XRP — toutes contre USDT | Les cinq plus échangées. Un marché très actif donne des données propres et sans trous |
| Pas de temps | 1 heure | À la minute, 3 millions de lignes par paire et beaucoup de bruit. À la journée, seulement 2 200 lignes : trop peu pour entraîner un modèle |
| Période | depuis septembre 2020 | C'est SOL qui limite : la paire n'existe sur Binance que depuis août 2020 |

> **Une précision sur les paires.** On parle souvent de « BTC/USD », mais **ce
> marché n'existe pas sur Binance**. Le dollar y est remplacé par l'USDT, une
> crypto-monnaie dont la valeur suit le dollar. Toutes nos paires sont donc en
> USDT.

---

## 2. Comment nous récupérons les données

Binance propose deux moyens d'accès, et nous avons besoin des deux car ils ne
répondent pas à la même question.

| | API REST | WebSocket |
|---|---|---|
| Principe | On pose une question, on reçoit une réponse | On s'abonne, on reçoit en continu |
| Répond à | Que s'est-il passé avant ? | Que se passe-t-il maintenant ? |
| On s'en sert pour | Constituer l'historique qui servira à entraîner le modèle | Alimenter le modèle en direct une fois en production |

### Les étapes de la récupération

1. **On demande la liste des paires** à Binance pour vérifier qu'elles existent
   et sont bien ouvertes aux échanges.
2. **On récupère les bougies par paquets de 1 000**, la limite imposée par
   Binance. Pour six ans d'historique, il en faut environ 53 par paire.
3. **On enchaîne les paquets** en repartant à chaque fois de la dernière bougie
   reçue, jusqu'à rattraper aujourd'hui.
4. **On nettoie les données** (section 3) et on les enregistre.
5. **En parallèle, on écoute le direct** via le WebSocket pour les nouvelles
   bougies.

Toute cette logique est écrite **une seule fois et fonctionne pour n'importe
quelle paire**. Le nom de la crypto n'est écrit nulle part dans le code : c'est
un paramètre. Ajouter une sixième paire ne demande aucune ligne de code
supplémentaire.

```bash
python -m scripts.collect_history                                  # les 5 paires du projet
python -m scripts.collect_history --paires ADAUSDT --intervalle 4h # n'importe quelle autre
```

> **Deux pièges rencontrés.** Si on demande plus de 1 000 bougies, **Binance en
> renvoie 1 000 sans prévenir** — aucune erreur. C'est pour ça qu'on repart de
> la dernière bougie reçue et jamais d'un compteur, sinon on perdrait des
> données sans s'en apercevoir.
>
> Binance limite aussi le nombre d'appels par minute. Notre collecte complète
> n'utilise que **9 % de ce qui est autorisé**, on est donc très loin de la
> limite.

---

## 3. À quoi ressemblent les données

C'est la partie la plus importante : les données brutes de Binance **ne sont
pas utilisables telles quelles**.

**Ce que Binance envoie :**

```json
[1787918400000, "79604.12000000", "79727.36000000", "79306.11000000",
 "79421.05000000", "400.05317000", 1787921999999, "31801379.67835110",
 113842, "172.31828000", "13699285.03758190", "0"]
```

**Après notre nettoyage :**

```json
{"symbol": "BTCUSDT", "interval": "1h",
 "open_time": "2026-08-28T12:00:00Z", "close_time": "2026-08-28T12:59:59Z",
 "open": 79604.12, "high": 79727.36, "low": 79306.11, "close": 79421.05,
 "volume": 400.05317, "quote_volume": 31801379.68, "nb_trades": 113842,
 "taker_buy_base": 172.31828}
```

Trois problèmes à régler :

1. **Aucun nom de colonne.** Il faut savoir que la 5ᵉ valeur est le prix de
   clôture. Nous nommons chaque colonne.
2. **Les prix sont du texte, pas des nombres.** Binance les envoie entre
   guillemets pour ne pas perdre de précision. Mais tant qu'ils restent du
   texte, aucun calcul n'est possible : l'ordinateur considère que `"9000"` est
   plus grand que `"79600"`, comme dans un dictionnaire. Nous les convertissons
   en nombres.
3. **Les dates sont des nombres géants** (millisecondes depuis 1970). Nous les
   convertissons en vraies dates, en précisant le fuseau UTC — sans ça, tout
   l'historique se décale de deux heures.

### Le direct pose un problème en plus

Le WebSocket renvoie la bougie en cours **à chaque fois qu'elle change**, soit
une quinzaine de fois par minute. Une seule de ces versions est la bonne :
celle où la bougie est terminée, signalée par un indicateur `x`.

> **Mesuré sur 200 secondes : 500 messages reçus, 20 bougies réellement
> gardées.** Sans ce filtre, on enregistrerait 96 % de doublons et des prix qui
> n'ont jamais été confirmés.

### L'organisation retenue

```
   API REST  ──┐
 (l'historique)│
               ├──>  Nettoyage  ──>  Fichiers prêts pour l'étape 2
               │   (même format
  WebSocket  ──┘    pour les deux)
   (le direct)
```

Les deux sources arrivent dans des formats différents mais ressortent
identiques. La suite du projet n'a donc qu'un seul format à gérer.

---

## 4. Ce que nous avons obtenu

| Résultat | Valeur |
|---|---|
| Bougies collectées | 262 415 |
| Période couverte | sept. 2020 → août 2026 |
| Temps de collecte | 81 secondes |
| Lignes en double | 0 |
| Valeurs manquantes | 0 |
| Bougies absentes | 20 par paire |

### Les 20 bougies manquantes

Nous avons cherché d'où venaient ces trous. Ils correspondent à **dix
interruptions**, principalement fin 2020 et courant 2021.

> **Le point important : ces dix interruptions tombent exactement aux mêmes
> dates sur les cinq paires.** Si notre programme était en cause, les trous
> seraient différents d'une paire à l'autre. Le fait qu'ils soient identiques
> montre qu'il s'agit d'**arrêts de Binance** (maintenances), pas d'un problème
> de notre côté.

Conséquence : il ne faudra **pas** inventer de prix pour combler ces trous. Le
marché était réellement fermé ; remplir ces heures créerait une information qui
n'a jamais existé.

---

## 5. Questions pour la suite

1. **Sur quelle durée veut-on prédire ?** Une décision d'achat à 1 h, 4 h ou
   24 h ? C'est ce qui déterminera ce que le modèle doit apprendre, et il vaut
   mieux le fixer avant l'étape 3.
2. **Le pas d'une heure convient-il ?** Ou faut-il aussi collecter en 15 minutes
   pour comparer ?
3. **Que fait-on des dix interruptions ?** Les laisser telles quelles, ou les
   signaler explicitement dans la base ?

---

## Livrables

| Livrable | Fichier |
|---|---|
| Rapport explicatif (PDF) | `docs/rapport_etape1.md` — ce document |
| Exemple de données collectées | `samples/exemple_donnees_binance.json` |
| Code de collecte | `src/`, `scripts/` |
