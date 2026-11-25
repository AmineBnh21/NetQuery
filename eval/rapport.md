# Rapport d'évaluation

Généré le 2026-11-25 13:55, 66 questions (`eval/questions.yaml`), modèle local via Ollama.

| Version | Questions | Exactitude | Latence moyenne | Latence médiane | Tokens prompt moyens | Questions corrigées |
|---|---|---|---|---|---|---|
| naive | 66 | **29 %** | 16.3 s | 12.9 s | 2732 | 0 |
| sans_rag | 66 | **62 %** | 13.9 s | 12.3 s | 1278 | 4 |
| optimise | 66 | **67 %** | 17.0 s | 15.8 s | 1285 | 3 |

## Exactitude par catégorie

| Catégorie | naive | sans_rag | optimise |
|---|---|---|---|
| anciennete | 0/6 | 3/6 | 5/6 |
| deploiement | 5/12 | 9/12 | 9/12 |
| geographie | 2/10 | 7/10 | 8/10 |
| outre-mer | 0/4 | 2/4 | 3/4 |
| pannes | 2/10 | 7/10 | 7/10 |
| parc | 3/6 | 4/6 | 4/6 |
| techno | 6/10 | 6/10 | 6/10 |
| temps | 1/8 | 3/8 | 2/8 |

## Types d'erreur

`resultat` : la requête s'exécute mais le résultat diffère de la référence ; `execution` : erreur SQL ; `securite` : refusée par les garde-fous ; `extraction` : pas de SQL dans la réponse.

| Version | resultat | execution | securite | extraction | llm |
|---|---|---|---|---|---|
| naive | 25 | 22 | 0 | 0 | 0 |
| sans_rag | 24 | 1 | 0 | 0 | 0 |
| optimise | 22 | 0 | 0 | 0 | 0 |

## Echecs de la version `optimise`

- **p04** (resultat) Combien de sites Free différents ont été en panne au moins un jour sur toute la période disponible ?
- **p09** (resultat) Combien de sites avaient à la fois la voix et la data hors service hier ?
- **p10** (resultat) Combien de sites Orange n'avaient plus de service voix hier ?
- **t05** (resultat) Pour chaque technologie, combien de sites avaient au moins un service en panne hier ?
- **t06** (resultat) Quelle technologie comptait le plus de sites en panne hier ?
- **g02** (resultat) Combien de sites étaient en panne hier dans le Rhône ?
- **g10** (resultat) Combien de départements comptaient au moins un site Free en panne hier ?
- **d03** (resultat) Combien de sites Orange étaient en panne en moyenne par jour en novembre 2026 ?
- **d04** (resultat) Nombre moyen de sites en panne par jour de la semaine
- **d05** (resultat) Combien de sites différents ont été en panne chaque semaine ISO ?
- **d06** (resultat) Combien de sites différents ont été en panne le week-end dernier ?
- **d07** (resultat) Combien de sites Bouygues Telecom étaient en panne à la fois le 1er novembre et le 24 novembre 2026 ?
- **d08** (resultat) Quelle est la différence entre le nombre de sites en panne hier et avant-hier ?
- **e04** (resultat) Combien d'émetteurs Free fonctionnent dans la bande 3500 MHz ?
- **e06** (resultat) Nombre d'émetteurs 5G mis en service chaque année
- **e08** (resultat) Combien d'émetteurs sont au statut projet approuvé, par opérateur ?
- **s02** (resultat) Quelle est la hauteur moyenne des supports par opérateur ?
- **s06** (resultat) Sur combien de supports différents SFR a-t-il installé des sites ?
- **a04** (resultat) Quel pourcentage des sites en panne hier l'était à cause d'un incident ?
- **o02** (resultat) Combien de sites chaque opérateur a-t-il à La Réunion ?
- **t07** (resultat) Combien de sites avaient la voix 3G en panne alors que la voix 4G fonctionnait hier ?
- **t10** (resultat) Nombre de sites SFR en panne 4G chaque jour de la semaine dernière
