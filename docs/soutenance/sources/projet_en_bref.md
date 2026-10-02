---
titre: CryptoBot, le projet en bref
sous_titre: Ce que nous avons construit, expliqué sans jargon technique
fichier: CryptoBot_projet_en_bref
modele: fiche
pages_max: 2
---

## L'idée de départ

Le prix d'une cryptomonnaie comme le bitcoin change à chaque seconde. Nous nous sommes posé une question simple : un programme peut-il apprendre, à partir de l'historique des prix, à deviner si le prix va monter ou baisser dans les minutes ou les heures qui suivent ? Et si oui, peut-il en tirer des décisions d'achat ou de vente ?

Pour y répondre, nous avons construit CryptoBot : un robot qui observe le marché en continu, donne son avis (acheter, vendre ou attendre) et tient un carnet de transactions fictives pour mesurer ce qu'il aurait gagné ou perdu. Aucun argent réel n'est engagé : c'est un projet de formation.

## Ce que nous avons fait, en cinq étapes

1. Récupérer les données. Nous avons collecté six ans d'historique de prix pour cinq cryptomonnaies parmi les plus échangées, auprès de Binance, la principale plateforme d'échange. Cela représente un peu plus de deux millions de relevés, tous vérifiés : aucun doublon, aucune valeur aberrante. Les nouveaux prix continuent d'arriver chaque quart d'heure.

2. Les ranger. Les données sont stockées dans deux bases : l'une garde les réponses de Binance telles qu'elles sont arrivées, comme une archive ; l'autre contient les données nettoyées, prêtes à l'emploi. Chaque donnée propre peut être rattachée à son original.

3. Apprendre au programme à prédire. Nous avons entraîné un modèle d'intelligence artificielle sur le passé, puis nous l'avons testé sur une période qu'il n'avait jamais vue, comme un examen sur des sujets inconnus. Le modèle a le droit de ne pas se prononcer quand il n'est pas assez sûr de lui.

4. Le rendre utilisable. Le modèle est accessible par une interface qui ressemble à celle des plateformes de trading : on y voit les prix en direct, l'avis du modèle et le résultat de chaque transaction fictive. L'accès est protégé, et l'ensemble démarre en une seule commande sur n'importe quel ordinateur.

5. Le faire tourner tout seul. Le système récupère les nouveaux prix, met à jour son carnet et se surveille sans intervention humaine. Chaque semaine, il entraîne un nouveau modèle et ne le met en service que s'il fait au moins aussi bien que l'ancien. Un tableau de bord montre en permanence que tout fonctionne, et signale le moindre problème.

## Ce que nous avons obtenu

Quand il se prononce, le modèle a raison environ 6 fois sur 10, alors que le hasard ferait 5 fois sur 10. C'est l'objectif que nous nous étions fixé.

Pourtant, il ne gagne pas d'argent. Sur un quart d'heure, le prix ne bouge en moyenne que d'environ 0,2 %, et chaque achat suivi d'une vente coûte justement 0,2 % de frais à la plateforme. Avoir raison un peu plus souvent que le hasard ne suffit pas à couvrir ces frais : il faudrait avoir raison presque à chaque fois. Nous avons préféré présenter ce résultat honnêtement plutôt que de chercher une période favorable qui l'aurait embelli.

## Ce que nous retenons

- Vérifier avec soin. Nos trois résultats les plus spectaculaires se sont révélés faux à la vérification. L'un d'eux venait d'une erreur subtile : le modèle voyait, sans que nous le voulions, une partie de l'avenir.
- Mesurer après les coûts. Un bon taux de réussite ne dit pas si une stratégie rapporte. Seul le calcul frais compris le dit.
- Automatiser et surveiller. Une grande partie du travail ne se voit pas : faire en sorte que tout tourne seul, de façon fiable, et qu'on soit averti quand quelque chose se dérègle.

La suite logique serait d'apprendre au modèle à ne viser que les mouvements de prix assez grands pour couvrir les frais.
