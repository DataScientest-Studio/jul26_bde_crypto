---
titre: Machine learning
sous_titre: Un modèle qui prédit le sens de la prochaine bougie, mesuré sur des données qu'il n'a jamais vues
etape: 3
fichier: CryptoBot_etape3_machine_learning
modele: fiche
---

## Ce que l'étape demandait

Entraîner plusieurs modèles sur les données stockées, les comparer, optimiser le meilleur et l'exporter pour l'API, en suivant les expériences avec MLflow.

## Nos choix et pourquoi

| Choix | Pourquoi | Alternative écartée |
|---|---|---|
| Prédire le sens de la prochaine bougie (day trading) | cible simple et mesurable, objectif fixé à 60 % de bonnes réponses | le prix, où un modèle naïf atteint un R² de 0,999998 ; les trois barrières, au sens juste à 50,6 % |
| Un modèle qui peut s'abstenir | il n'agit qu'au-delà d'un seuil de probabilité, réglé par style (agressif ou conservateur) | se prononcer sur chaque bougie |
| Extrait figé signé (SHA-256) | la base change chaque jour ; le même script doit donner le même modèle | entraîner sur la base vivante |
| Découpage chronologique | apprendre sur le passé, tester sur le futur | un découpage aléatoire |
| 35 variables sans unité, avec le contexte 1 h et 4 h | un prix brut appris ne vaut ni pour une autre paire ni l'année suivante | les 86 colonnes de la bibliothèque `ta`, très redondantes |
| Forêt aléatoire calibrée | meilleure en validation ; la calibration ajoute 1,5 à 1,7 point | gradient boosting, régression logistique |
| Une règle fixée avant les essais | une piste doit progresser à 2 % et à 5 % d'activité | garder ce qui marche sur une seule période |

Cette rigueur a écarté trois faux résultats, dont une fuite de données : la bougie 4 h jointe n'était pas encore clôturée, et le score passait de 0,52 à 0,61. Un test automatisé l'empêche de revenir.

## Résultat

- Sur 14 831 bougies jamais vues : 59,7 % de bonnes réponses en style agressif, 59,9 % en conservateur (intervalle à 95 % de 55,4 % à 64,3 %), avec 20 à 25 ordres par jour.
- Modèle exporté en `.joblib` avec ses 35 variables, ses seuils et l'empreinte de ses données ; chaque entraînement est tracé dans MLflow.

## Limite

Le modèle a raison environ 60 % du temps mais n'est pas rentable : en 15 minutes, le prix bouge en moyenne de 0,227 % pour 0,2 % de frais par aller-retour, il faudrait 94 % de précision. Au backtest, il perd 3,6 à 5,0 % frais compris. La piste suivante est de ne prédire que les mouvements assez grands pour couvrir les frais.
