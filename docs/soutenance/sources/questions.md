---
titre: Questions probables et réponses
sous_titre: Les réponses courtes à avoir en tête
fichier: CryptoBot_questions_reponses
modele: document
sommaire: 2-3
---

## Projet

### Pouvez-vous résumer le projet ?

On collecte les bougies de 5 paires Binance, on les stocke dans deux bases, et un modèle dit s'il faut acheter, vendre ou attendre. Le tout est servi par une API, tourne seul avec Airflow et est surveillé avec Grafana.

### Pourquoi ces cinq paires ?

Ce sont parmi les plus échangées, donc les bougies sont régulières et sans trous. Cinq suffisent pour vérifier que le modèle marche sur plusieurs actifs.

## Collecte et bases

### Pourquoi REST et WebSocket ?

Le REST sert pour l'historique, le WebSocket pour le temps réel. Les deux arrivent dans le même format, donc la suite ne voit pas la différence.

### Comment respectez-vous les limites de Binance ?

On lit le quota consommé dans chaque réponse et on fait une pause avant la limite. On demande toujours 1 000 bougies par appel, le maximum.

### Comment gérez-vous les bougies manquantes ?

On les mesure mais on ne les remplit pas. Le marché était fermé, donc remplir inventerait des prix.

### Pourquoi deux bases ?

Les bougies ont toujours les mêmes colonnes, elles vont dans PostgreSQL. Les réponses brutes de Binance ont une structure variable, elles vont dans MongoDB, ce qui permet de rejouer un nettoyage.

### Votre chargement est-il idempotent ?

Oui, on met à jour sur la clé de la bougie au lieu d'insérer. Relancer un chargement ne crée jamais de doublon.

## Machine learning

### Comment avez-vous préparé les données ?

On part d'un extrait figé, on calcule 35 indicateurs sans unité paire par paire, on remplace les manques par la médiane et on normalise. Le découpage suit l'ordre du temps : le passé pour apprendre, le futur pour tester.

### Quelles sont les métriques en entraînement et en test ?

68,4 % de bonnes réponses en entraînement contre 52,4 % en test, et 59,9 % en test quand le modèle se prononce. L'écart montre que la forêt apprend un peu de bruit, c'est pour ça qu'on ne la juge que sur le test.

### Pourquoi pas le R² ou la MAPE ?

Ce sont des métriques de régression. Sur le prix, le modèle naïf « prix suivant = prix actuel » a un R² de 0,99999, ce qui ne veut rien dire. On a donc prédit le sens de la bougie.

### Pourquoi une forêt aléatoire ?

Elle a eu le meilleur score en validation face au gradient boosting et à la régression logistique. Elle demande peu de réglages.

### Parlez-nous de la fuite de données.

Une bougie de 12 h 15 recevait les infos d'une bougie 4 h qui se termine à 16 h. Le score est monté de 0,52 à 0,61, c'est ce qui nous a alertés. On l'a corrigée et un test empêche qu'elle revienne.

### 60 %, ce n'est pas peu ?

Le hasard est à 50 % et l'intervalle de confiance va de 55 à 64 %. Le vrai problème, ce sont les frais.

## API et Docker

### Pourquoi une clé d'API et pas JWT ?

Nos clients sont des programmes, pas des humains qui se connectent. La clé d'API est le standard pour ce cas.

### Pourquoi la clé n'est-elle pas dans le navigateur ?

Tout ce qui va dans le navigateur peut être lu. C'est nginx qui ajoute la clé côté serveur.

### Pourquoi pas de HTTPS ?

Tout est lié à 127.0.0.1, rien n'est joignable depuis le réseau. On l'ajouterait avant toute ouverture.

### Pourquoi Docker Compose et pas Kubernetes ?

On a une seule machine. Compose démarre les 16 services en une commande, Kubernetes ne servirait qu'avec plusieurs serveurs.

## Automatisation et supervision

### Pourquoi Airflow plutôt que cron ?

Airflow gère l'ordre des tâches, les relances en cas d'échec et garde l'historique de chaque exécution.

### Comment le modèle est-il remplacé ?

Le nouveau modèle affronte l'ancien sur les 21 derniers jours. Il ne le remplace que s'il fait au moins aussi bien sur au moins 30 ordres.

### Comment mesurez-vous la dérive ?

Avec l'indice PSI, par paire et par pas de temps. Au-dessus de 0,25 la dérive est forte, mais ça ne veut pas dire que le modèle est moins bon.

### Que vérifie la CI ?

Le code, les 149 tests, les DAG, les configurations et un démarrage complet sur une machine vierge. Les images ne sont publiées que si tout passe.

### Les alertes préviennent-elles quelqu'un ?

Non, elles se lisent dans Grafana. La suite serait Alertmanager.

## Rentabilité

### Le bot est-il rentable ?

Non. Une bougie de 15 minutes bouge de 0,227 % et les frais coûtent 0,2 %. Il faudrait 94 % de bonnes réponses, le backtest est à moins 3,6 %.

### Le bot vend à découvert, c'est possible en spot ?

Non, c'est une limite de la simulation. Le projet est académique, aucun ordre réel n'est passé.

### Que feriez-vous avec plus de temps ?

Ne prédire que les gros mouvements qui couvrent les frais. Ajouter les notifications d'alerte et sortir d'une seule machine.

## Si on ne sait pas

« On ne l'a pas mesuré, donc je préfère ne pas donner de chiffre au hasard. Voici comment on le mesurerait. »

« Cette partie a surtout été faite par mon binôme, il pourra compléter. »
