---
titre: Support de présentation
sous_titre: Ce que nous disons sur chaque slide
fichier: CryptoBot_support_presentation
modele: document
sommaire: 2
---

## Déroulé

Ulrich parle de la collecte, des bases, de la fiabilité et de la CI. Célian parle de l'architecture, du modèle, de l'API, de la démo et de l'automatisation. Celui qui ne parle pas change les slides.

| Slide | Sujet | Durée | Présentateur |
|---|---|---|---|
| 1 | Titre | 0 min 30 | Ulrich |
| 2 | Le projet | 1 min 00 | Ulrich |
| 3 | Architecture globale | 1 min 30 | Célian |
| 4 | Collecter les données | 1 min 15 | Ulrich |
| 5 | La bonne base pour la bonne forme | 1 min 30 | Ulrich |
| 6 | Apprendre sans tricher | 2 min 00 | Célian |
| 7 | Le résultat, sans l'enjoliver | 1 min 30 | Célian |
| 8 | Du notebook au service protégé | 1 min 30 | Célian |
| 9 | Ce qui fait tenir le service | 1 min 15 | Ulrich |
| 10 | Démonstration en direct | 3 min 00 | Célian |
| 11 | Tourner seul, remplacer avec preuve | 1 min 30 | Célian |
| 12 | Du commit à la supervision | 1 min 30 | Ulrich |
| 13 | Bilan | 1 min 30 | Célian |
| 14 | Merci, questions | 0 min 30 | Célian |
| | Total | 20 min 00 | |

La démo doit commencer vers 12 min. Si on est en retard, on raccourcit les slides 11 et 12.

## Slide 1 : CryptoBot

Ulrich. CryptoBot est un bot de trading crypto piloté par un modèle de machine learning. Le projet va de la collecte des données jusqu'à l'automatisation complète. On va expliquer chaque choix, et aussi nos erreurs.

Transition : « Commençons par la question à laquelle le projet répond. »

## Slide 2 : Le projet

Ulrich. La question est simple : la prochaine bougie va-t-elle monter ou baisser ? Le modèle donne une probabilité, puis le bot décide d'acheter, vendre ou attendre. Le style conservateur attend d'être plus sûr que le style agressif.

Transition : « Célian va vous montrer comment tout s'assemble. »

## Slide 3 : Architecture globale

Célian. Tout tourne dans Docker Compose sur une seule machine, 16 services en une commande. Binance alimente deux bases, Airflow automatise, l'API sert le modèle et l'interface l'affiche. La CI livre le code et Grafana surveille l'ensemble.

Transition : « Je laisse Ulrich vous parler de la collecte. »

## Slide 4 : Collecter les données

Ulrich. Le REST donne l'historique et le WebSocket le temps réel. On respecte les limites de Binance en lisant le quota dans chaque réponse. On ne garde que les bougies terminées, et on obtient deux millions de bougies sans doublon.

Transition : « Ces bougies, il faut maintenant les ranger. »

## Slide 5 : La bonne base pour la bonne forme

Ulrich. Les bougies ont toujours les mêmes colonnes, donc elles vont dans PostgreSQL avec TimescaleDB. Les réponses brutes de Binance vont dans MongoDB, telles quelles, pour pouvoir rejouer un nettoyage. Le chargement ne crée jamais de doublon, même relancé.

Transition : « Célian va vous expliquer comment on a entraîné le modèle. »

## Slide 6 : Apprendre sans tricher

Célian. On apprend sur le passé et on teste sur le futur, sur 14 831 bougies que le modèle n'a jamais vues. Le tableau montre que le modèle fait 68 % en entraînement mais 52 % en test : il apprend un peu de bruit, donc on le juge seulement sur le test. Quand il se prononce, il a raison 59,9 % du temps. On a aussi trouvé et corrigé une fuite du futur, qui faisait monter le score de 0,52 à 0,61.

Transition : « Voyons ce que vaut ce modèle, sans l'enjoliver. »

## Slide 7 : Avoir raison ne suffit pas

Célian. L'objectif de 60 % de bonnes réponses est atteint. Mais une bougie de 15 minutes bouge de 0,227 % et les frais coûtent 0,2 %. Résultat, le backtest est à moins 3,6 % : le modèle a raison, mais les frais mangent le gain.

Transition : « Il faut maintenant sortir ce modèle du notebook. »

## Slide 8 : Du notebook au service protégé

Célian. Le modèle est servi par une API FastAPI. Chaque client a sa propre clé, et c'est nginx qui l'ajoute pour qu'elle ne passe jamais par le navigateur. Sans clé configurée, l'API refuse tout.

Transition : « Ulrich va vous montrer ce qui rend le service fiable. »

## Slide 9 : Ce qui fait tenir le service

Ulrich. On a 149 tests qui tournent en quelques secondes, dont un qui vérifie que chaque route demande une clé. Le modèle est monté à part, donc on le change sans reconstruire l'image. On mesure aussi la dérive des données avec le PSI.

Transition : « Célian va vous faire la démonstration. »

## Slide 10 : Démonstration en direct

Célian. Voici l'interface avec les bougies Binance en direct et la décision du bot. Le carnet montre les positions virtuelles avec leur take profit et leur stop loss. Rien n'est vraiment acheté, c'est une simulation.

Avant la soutenance, ouvrir l'interface (localhost:8080), Grafana (localhost:3000), Airflow (localhost:8088) et MLflow (localhost:5000). Régler l'interface sur BTCUSDT, 1h, Conservateur, 1 semaine.

| Temps | Ce qu'on fait | Ce qu'on dit |
|---|---|---|
| 0:00 | Rien | La dernière bougie arrive en direct |
| 0:30 | Montrer la décision | Le bot donne une probabilité, le plus souvent il attend |
| 0:55 | Cliquer sur Agressif | Même modèle, il agit plus souvent |
| 1:20 | Période 1 mois | Le carnet montre les trades, frais déduits |
| 2:20 | SOLUSDT puis 15m | Le même modèle marche sur les autres paires |
| 2:45 | Revenir à 1h | Airflow fait tourner le bot toutes les 15 minutes |

Si ça plante : passer sur « Base du projet » dans le menu Données, sinon montrer Grafana, sinon les captures d'écran. Ne pas chercher à corriger plus de 20 secondes.

Transition : « Tout ça tourne sans nous, passons à l'automatisation. »

## Slide 11 : Tourner seul, remplacer avec preuve

Célian. Airflow lance la collecte et le bot toutes les 15 minutes, la dérive chaque matin et le réentraînement le dimanche. Un nouveau modèle ne remplace l'ancien que s'il fait au moins aussi bien sur les 21 derniers jours. Pour l'instant, l'ancien a toujours gardé sa place.

Transition : « Ulrich va vous parler de la livraison du code et de la supervision. »

## Slide 12 : Du commit à la supervision

Ulrich. À chaque push, GitHub Actions lance cinq contrôles, dont un démarrage complet sur une machine vierge. Si tout passe, les images sont publiées et Watchtower les installe. Prometheus et Grafana surveillent le service, les données, le bot et le modèle avec 12 alertes.

Transition : « Célian va conclure. »

## Slide 13 : Bilan

Célian. La chaîne complète fonctionne seule, de la collecte jusqu'à la supervision. On retient trois leçons : valider dans le temps, mesurer après les frais, et surveiller les données. La suite serait de ne viser que les gros mouvements et de sortir d'une seule machine.

Transition : « Il me reste à vous remercier. »

## Slide 14 : Merci, questions

Célian. Merci pour votre écoute. Ulrich répond sur la collecte, les bases, les tests et la CI. Célian répond sur l'architecture, le modèle, l'API et l'automatisation.

## À ne pas dire

- Ne jamais dire que le bot est rentable.
- Ne pas dire que l'API est totalement sécurisée : il n'y a pas de HTTPS.
- Ne pas dire qu'Airflow entraîne le modèle : il lance nos scripts.
- Ne pas lire les slides.
