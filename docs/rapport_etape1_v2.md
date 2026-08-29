# Étape 1 — Récupération des données Binance

**Version 2** — 29 août 2026
**Projet CryptoBot** — cursus Data Engineer
**Dépôt** : `DataScientest-Studio/jul26_bde_crypto`

> La [version 1](rapport_etape1_v1.md) reste consultable telle qu'elle a été
> rendue. Ce document la remplace et décrit en section 5 ce qui a changé et
> pourquoi.

## Historique des versions

| Version | Date | Périmètre | Ce qui a motivé le changement |
|---|---|---|---|
| v1 | 28 août 2026 | Un seul pas de temps : 1 heure, depuis sept. 2020 | Livrable initial de l'étape 1 |
| **v2** | **29 août 2026** | **7 pas de temps répartis sur 3 profils de trading** | Un bot ne regarde pas le marché à la même échelle selon la stratégie visée |

---

| | v1 | v2 |
|---|---:|---:|
| Jeux de données | 5 | **35** |
| Pas de temps | 1 | **7** |
| Lignes collectées | 262 415 | **2 075 570** |
| Volume (Parquet) | 20,2 Mo | **131,8 Mo** |
| Durée de collecte | 81 s | **637 s** |

---

## 1. Ce que nous collectons

Nous récupérons l'historique des cours de cinq crypto-monnaies sur Binance,
sous forme de **bougies**. Une bougie résume une tranche de temps en quelques
chiffres : prix d'ouverture, plus haut, plus bas, prix de clôture, et volume
échangé.

| Choix | Valeur | Pourquoi |
|---|---|---|
| Plateforme | Binance | Premier exchange mondial, API publique gratuite, aucune inscription nécessaire pour les données de marché |
| Paires | BTC, ETH, BNB, SOL, XRP — toutes contre USDT | Les cinq plus échangées. Un marché très actif donne des données propres |
| Pas de temps | 7, selon le profil de trading | Voir ci-dessous |
| Période | variable selon le pas de temps | Voir ci-dessous |

> **Une précision sur les paires.** On parle souvent de « BTC/USD », mais **ce
> marché n'existe pas sur Binance**. Le dollar y est remplacé par l'USDT, une
> crypto-monnaie dont la valeur suit le dollar. Toutes nos paires sont donc en
> USDT.

### Les trois profils de trading

C'est l'apport principal de cette version. Un bot de trading ne regarde pas le
marché à la même échelle selon la stratégie visée : un scalpeur travaille à la
minute, un swing trader à la semaine. Chaque profil définit donc ses pas de
temps **et** sa profondeur d'historique.

| Profil | Pas de temps | Historique | Pourquoi cette profondeur |
|---|---|---|---|
| **Scalping** | 1m, 5m, 15m | 180 jours | Positions de quelques minutes. Au-delà de quelques mois, la façon dont le marché s'échange a trop changé pour rester pertinente à cette échelle |
| **Day trading** | 15m, 1h, 4h | 730 jours | Positions ouvertes et fermées dans la journée. Deux ans couvrent plusieurs régimes de marché — hausse, baisse, stagnation — sans noyer le modèle sous le bruit |
| **Swing** | 4h, 1d, 1w | tout (depuis sept. 2020) | Positions tenues plusieurs jours à plusieurs semaines. Une bougie hebdomadaire ne produit que ~50 lignes par an : il faut tout l'historique pour avoir de quoi entraîner |

**Un point de conception à expliquer.** Le pas `15m` est réclamé par deux
profils, mais sur des profondeurs différentes : 180 jours pour le scalping,
730 pour le day trading. Nous retenons **la plus longue**. Collecter large
satisfait les deux profils ; l'inverse laisserait le day trading à court de
données. Même logique pour `4h`, partagé entre day trading et swing, qui hérite
donc de tout l'historique.

Le résultat n'est pas 9 collectes (3 profils × 3 pas de temps) mais **7**, une
par pas de temps distinct.

### Pourquoi l'historique démarre en septembre 2020

C'est **SOL** qui limite : la paire n'existe sur Binance que depuis août 2020.
BTC et ETH remontent à 2017, mais aligner les cinq paires sur une base commune
est indispensable dès qu'on voudra comparer les cryptos entre elles à
l'étape 3. Un historique plus long sur BTC seul aurait produit des trous béants
sur les autres colonnes.

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
2. **On détermine quoi collecter** à partir du ou des profils demandés : quels
   pas de temps, et depuis quelle date pour chacun.
3. **On récupère les bougies par paquets de 1 000**, la limite imposée par
   Binance. Pour le pas d'une minute sur 180 jours, il faut 260 paquets par
   paire.
4. **On enchaîne les paquets** en repartant à chaque fois de la dernière bougie
   reçue, jusqu'à rattraper aujourd'hui.
5. **On nettoie les données** (section 3) et on les enregistre.
6. **En parallèle, on écoute le direct** via le WebSocket pour les nouvelles
   bougies.

Toute cette logique est écrite **une seule fois et fonctionne pour n'importe
quelle paire et n'importe quel pas de temps**. Ni le nom de la crypto ni
l'intervalle ne sont écrits en dur : ce sont des paramètres.

```bash
python -m scripts.collect_history --list              # décrit les profils
python -m scripts.collect_history --profile scalping
python -m scripts.collect_history --profile all
python -m scripts.collect_history --pairs ADAUSDT --intervals 4h
```

> **Deux pièges rencontrés.** Si on demande plus de 1 000 bougies, **Binance en
> renvoie 1 000 sans prévenir** — aucune erreur. C'est pour ça qu'on repart de
> la dernière bougie reçue et jamais d'un compteur, sinon on perdrait des
> données sans s'en apercevoir.
>
> Binance limite aussi le nombre d'appels par minute. Même en collectant les
> 35 jeux de données, on reste très loin de la limite : le coût d'un appel est
> le même qu'on demande 1 bougie ou 1 000, donc on demande toujours 1 000.

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

1. **Aucun nom de colonne.** Binance envoie toujours ses 12 valeurs dans le
   même ordre — c'est un contrat. On déclare la liste des noms une seule fois
   dans le code, et chaque nom se colle sur sa position.
2. **Les prix sont du texte, pas des nombres.** Binance les envoie entre
   guillemets pour ne pas perdre de précision. Mais tant qu'ils restent du
   texte, aucun calcul n'est possible : l'ordinateur considère que `"9000"` est
   plus grand que `"79600"`, comme dans un dictionnaire.
3. **Les dates sont des nombres géants** (millisecondes depuis 1970). Nous les
   convertissons en vraies dates, en précisant le fuseau UTC — sans ça, tout
   l'historique se décale de deux heures.

Un quatrième point, moins visible : **la réponse ne contient ni la paire ni le
pas de temps**. L'API suppose qu'on se souvient de sa propre question. Nous
ajoutons donc ces deux colonnes nous-mêmes — sans elles, impossible d'empiler
35 jeux de données dans une même table à l'étape 2.

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

**2 075 570 lignes** réparties sur **35 jeux de données** (7 pas de temps ×
5 paires), collectées en **10 min 37 s**.

| Pas de temps | Depuis | Jeux | Lignes | Complétude | Parquet |
|---|---|---:|---:|---:|---:|
| 1m | 2026-03-02 | 5 | 1 299 275 | 100 % | 74,8 Mo |
| 5m | 2026-03-02 | 5 | 259 860 | 100 % | 17,7 Mo |
| 15m | 2024-08-29 | 5 | 350 620 | 100 % | 25,9 Mo |
| 1h | 2024-08-29 | 5 | 87 655 | 100 % | 7,0 Mo |
| 4h | 2020-09-01 | 5 | 65 655 | 100 % | 5,3 Mo |
| 1d | 2020-09-01 | 5 | 10 945 | 100 % | 0,9 Mo |
| 1w | 2020-09-01 | 5 | 1 560 | 100 % | 0,2 Mo |
| **Total** | | **35** | **2 075 570** | | **131,8 Mo** |

Zéro doublon, zéro valeur nulle, zéro incohérence de prix sur les 35 jeux.

### Pourquoi 100 % ici, et 99,96 % en v1

La v1 annonçait 20 bougies manquantes par paire, correspondant à dix arrêts de
la plateforme Binance. **Ces arrêts n'ont pas disparu** — mais ils ne sont plus
visibles, pour deux raisons distinctes.

**Première raison : la fenêtre a changé.** Les dix pannes datent de 2020-2021 et
de mars 2023. Le pas horaire démarre désormais en août 2024 : il passe
simplement à côté. Ce n'est pas une amélioration de la qualité, juste une
période différente.

**Seconde raison, plus intéressante : les pannes sont absorbées par les pas de
temps longs.** Le `4h` couvre pourtant bien 2021. Voici la panne du 25 avril
2021, où les heures 05:00, 06:00 et 07:00 sont absentes en pas horaire :

| Bougie 4h | Volume | Transactions |
|---|---:|---:|
| 00:00 | 6 421 BTC | 127 917 |
| **04:00** | **5,9 BTC** | **224** |
| 08:00 | 10 524 BTC | 274 159 |

La bougie de 04:00 **existe** — parce qu'il y a eu 224 transactions avant que
la plateforme lâche à 05:00. Binance produit donc une bougie 4 h, et le trou de
trois heures disparaît du décompte.

Il n'a pas disparu des données pour autant : il est **absorbé**, repérable
seulement à un volume mille fois inférieur à la normale.

> **Ce qu'il faut en retenir : la qualité mesurée dépend de l'échelle à
> laquelle on mesure.** Les mêmes données affichent 99,96 % en pas horaire et
> 100 % en 4 h, sans qu'aucune ligne n'ait changé. Un taux de complétude n'a de
> sens qu'accompagné de sa granularité.

**Conséquence pratique** : il ne faudra **pas** inventer de prix pour combler
les trous du pas horaire. Le marché était réellement fermé ; remplir ces heures
créerait une information qui n'a jamais existé. Et il faudra se méfier des
bougies à volume anormalement bas, qui signalent une panne sans la déclarer.

Pour reproduire les chiffres de la v1 :

```bash
python -m scripts.collect_history --intervals 1h --start 2020-09-01
```

---

## 5. Ce qui a changé depuis la version 1

### Les profils de trading

**Avant** : un seul pas de temps, 1 heure, choisi comme compromis entre le
bruit du pas minute et la rareté du pas journalier.

**Maintenant** : trois profils couvrant sept pas de temps. Le compromis de la
v1 n'était pas faux, mais il présupposait une stratégie de trading qui n'avait
jamais été décidée. Tant que l'horizon de prédiction n'est pas arrêté (voir
section 6), collecter les trois échelles laisse la décision ouverte au lieu de
la préempter.

Le coût est modéré : 10 minutes de collecte et 132 Mo au lieu de 81 secondes et
20 Mo.

### Trois défauts corrigés

**La clé de déduplication ignorait le pas de temps.** Avec un seul pas, le
problème était invisible. Dès qu'on collecte 1h et 4h, une bougie 1h et une
bougie 4h ouvertes à la même heure étaient considérées comme la même donnée, et
l'une écrasait l'autre. La clé inclut désormais le pas de temps.

**Le contrôle de complétude déduisait la durée d'une bougie depuis son nom.**
Cela fonctionnait pour `1h` et `15m`, mais `1w` aurait planté. Remplacé par une
table explicite couvrant les quatorze pas de temps de Binance.

**Le rapport qualité s'écrasait à chaque collecte.** Collecter une seule paire
effaçait le bilan de toutes les autres. Il fusionne désormais avec les
collectes précédentes.

### Une convention de code

Le code est passé **en anglais** — noms de variables, de fonctions, de colonnes
— tandis que **les commentaires et la documentation restent en français**.
C'est la convention habituelle en Python, et cela évite les mélanges du type
`normaliser_klines()`. Les commentaires servent à expliquer nos choix à
l'équipe et au jury : ils restent dans notre langue.

---

## 6. Questions pour la suite

1. **Sur quelle durée veut-on prédire ?** Une décision d'achat à 1 h, 4 h ou
   24 h ? C'est la question qui commande tout le reste : elle détermine la
   variable cible du modèle, et donc lequel des trois profils devient le profil
   principal. Elle devrait être tranchée avant l'étape 3.
2. **Faut-il conserver les trois profils, ou n'en garder qu'un ?** Les garder
   tous coûte 132 Mo et laisse toutes les options ouvertes. N'en garder qu'un
   simplifierait le modèle de données de l'étape 2.
3. **Que fait-on des dix interruptions ?** Les laisser telles quelles — notre
   recommandation — ou les signaler explicitement dans la base pour que le
   modèle sache qu'il ne s'agit pas de données normales ?

---

## Livrables

| Livrable | Fichier |
|---|---|
| Rapport v2 (PDF) | `docs/rapport_etape1_v2.pdf` — ce document |
| Rapport v1, archivé | `docs/rapport_etape1_v1.pdf` |
| Exemple de données collectées | `samples/exemple_donnees_binance.json` |
| Contrôle qualité des 35 jeux | `docs/rapport_qualite.json` |
| Code de collecte | `src/`, `scripts/` |
