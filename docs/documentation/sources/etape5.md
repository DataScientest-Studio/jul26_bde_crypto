---
titre: Automatisation et supervision
sous_titre: L'application tourne seule, se met à jour seule et prévient quand elle se dégrade
etape: 5
fichier: CryptoBot_etape5_automatisation
modele: fiche
---

## Ce que l'étape demandait

Faire fonctionner l'application en continu sans intervention : automatiser les étapes précédentes, mettre en place une chaîne d'intégration continue et superviser la production.

## Nos choix et pourquoi

| Choix | Pourquoi | Alternative écartée |
|---|---|---|
| Airflow orchestre, il ne calcule pas | chaque tâche lance un script du projet, testé ; une panne se reproduit avec une ligne de commande | mettre la logique dans les DAG |
| Exécuteur local, collecte décalée d'une minute | peu de tâches ; le décalage évite d'arriver avant la clôture de la bougie | Celery et sa file de messages |
| Champion contre challenger | un modèle réentraîné n'est pas forcément meilleur ; il ne remplace l'ancien que s'il fait au moins aussi bien sur 21 jours non vus | remplacer le modèle à chaque réentraînement |
| Registre MLflow, alias `champion` | garde la trace de ce qui a été essayé et de ce qui sert | des fichiers sans historique |
| CI GitHub Actions en 5 contrôles | analyse, tests, DAG, configuration, déploiement complet ; un DAG cassé disparaît d'Airflow sans message | publier sans vérifier |
| Watchtower en mode pull | la machine va chercher les images ; rien n'est ouvert vers l'extérieur | un exécuteur GitHub sur la machine : dépôt public, du code d'un fork pourrait y tourner |
| Prometheus et Grafana | Prometheus interroge chaque service : un service arrêté se voit tout de suite | consulter les journaux à la main |
| Un exportateur à part | l'état du pipeline (données, bot, réentraînement) est en base ; il le traduit en métriques et survit à une panne de l'API | ne superviser que l'API |

## Résultat

- 3 DAG : collecte et bot toutes les 15 minutes, dérive chaque matin, réentraînement chaque dimanche ; au premier lancement, 25 jours rattrapés en 15 secondes.
- Deux duels, gagnés par le champion (0,575 contre 0,558 le 23 septembre), dans les marges d'erreur. Le champion gardait 57,5 % de bonnes réponses malgré une dérive forte sur les 15 séries.
- 12 règles d'alerte, du service injoignable au réentraînement absent depuis 9 jours.

## Limite

Les alertes ne préviennent personne : elles se consultent dans Grafana et Prometheus. Tout tourne sur une seule machine, sans HTTPS, et l'automatisation entretient un modèle qui ne couvre pas ses frais.
