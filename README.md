# NetQuery

**Interroger les données des réseaux mobiles français en langage naturel.**

NetQuery est un assistant text-to-SQL : on pose une question en français
(« Combien de sites SFR étaient en panne 4G dans le Rhône la semaine dernière ? »),
l'outil génère la requête SQL, la vérifie, l'exécute et renvoie le résultat
sous forme de tableau, de graphique ou de carte.

Le projet vise un cas d'usage réaliste d'opérateur télécom : les données sont
dans un entrepôt cloud facturé à l'usage, donc l'outil doit produire des
requêtes **justes**, **sûres** et **peu coûteuses**.

## Objectifs

- Répondre à des questions métier sur la disponibilité, le déploiement et la qualité des réseaux mobiles.
- Garantir la sécurité : lecture seule, uniquement des `SELECT`, limites de volume et de temps.
- Réduire le coût et la latence : partitions, tables agrégées, estimation du coût avant exécution, caches.
- Mesurer tout cela sur un jeu d'évaluation (exactitude, octets scannés, latence, coût).

## Données

Toutes les sources sont publiques (open data, data.gouv.fr).

| Source | Contenu | Fréquence |
|---|---|---|
| [Arcep, Sites indisponibles](https://www.data.gouv.fr/datasets/sites-indisponibles) | Sites mobiles déclarés hors service par les opérateurs, avec l'état par service (voix, data) et par technologie | Quotidienne |
| [ANFR, Données sur les réseaux mobiles](https://www.data.gouv.fr/datasets/donnees-sur-les-reseaux-mobiles) | Stations 2G/3G/4G/5G : opérateur, localisation, date de mise en service, hauteur, azimuts | Hebdomadaire |
| [Arcep, Mon Réseau Mobile](https://www.data.gouv.fr/datasets/monreseaumobile) | Couverture, mesures de qualité de service (campagnes Arcep et crowdsourcing) | Trimestrielle / annuelle |
| [API Découpage administratif](https://geo.api.gouv.fr/decoupage-administratif) | Communes, départements, régions, population | Annuelle |

Vérifier la licence de chaque jeu de données sur sa page data.gouv.fr avant toute réutilisation publique.

## Architecture

```
Question (FR)
   │
   ▼
Sélection du schéma (RAG sur les descriptions de tables)
   │   + glossaire métier + exemples question → SQL similaires
   ▼
LLM : génération du SQL
   │
   ▼
Validation : sqlglot (SELECT uniquement), LIMIT, tables autorisées
   │
   ▼
Estimation du coût (dry run BigQuery / EXPLAIN) ── trop cher ? → refus ou confirmation
   │
   ▼
Exécution (utilisateur en lecture seule) ── erreur ? → correction par le LLM (2 essais max)
   │
   ▼
Résultat : tableau + graphique/carte + phrase de synthèse + SQL affiché
```

### Modèle de données

```
dim_operateur    (operateur_id, nom, est_metropole)                     -- ORANGE, SFR, BOUYGUES, FREE + outre-mer
dim_departement  (departement, nom, region_code, region)
dim_commune      (code_insee, nom, departement, departement_nom, region, population, ...)
dim_date         (date, annee, trimestre, mois, mois_nom, semaine_iso, jour_semaine, jour_nom, est_weekend)
dim_site         (site_id, operateur_id, support_id, code_insee, departement, lat, lon, hauteur_m,
                  date_mise_service, a_2g, a_3g, a_4g, a_5g, source)

fact_station       (emetteur_id, site_id, operateur_id, techno, systeme, bande_mhz, statut, est_actif,
                    date_mise_service, ...)                              -- ANFR, un émetteur par ligne
fact_indispo_site  (date, site_id, operateur_id, departement, code_insee, raison, voix_hs, data_hs,
                    debut, fin_prevue, nb_jours_depuis_debut, ...)       -- un site HS par jour
fact_indispo       (date, site_id, operateur_id, departement, code_insee, service, techno, est_hs, raison)
                                                                         -- format long : site x jour x service x techno

agg_indispo_dept_jour  (date, departement, operateur_id, service, techno, nb_sites_hs, nb_sites_declares)
agg_sites_hs_dept_jour (date, departement, operateur_id, nb_sites_indispo, nb_sites_voix_hs,
                        nb_sites_data_hs, nb_sites_incident, nb_sites_maintenance)
```

Choix de modélisation :

- Un **site** est une station ANFR (`sta_nm_anfr`), qui est aussi la clé `station_anfr` des sites indisponibles (99,7 % de correspondance). Les rares lignes Arcep sans station reçoivent un identifiant synthétique (`NA-...`) pour que chaque ligne de fait ait son site.
- Les faits portent `departement` en plus de `code_insee` : la plupart des questions filtrent par département, ce qui évite une jointure et servira de clé de clustering sur BigQuery.
- `fact_indispo` est non additif entre technologies (un site HS en 3G et en 4G compte deux lignes) : pour compter des sites, utiliser `count(distinct site_id)`, `fact_indispo_site` ou `agg_sites_hs_dept_jour`.
- Dans l'ANFR, les émetteurs 5G ont tous le statut « Techniquement opérationnel » et jamais « En service » : `est_actif` regroupe les deux statuts.

Les tables `agg_` servent à répondre aux questions courantes sans scanner les données détaillées.

## Structure du projet

```
netquery/
├── README.md
├── CLAUDE.md              # contexte du projet pour Claude Code
├── requirements.txt
├── scripts/
│   ├── ingest.py          # téléchargement + chargement DuckDB + profil du schéma
│   └── transform.py       # raw -> staging -> modèle, puis contrôles qualité
├── sql/
│   ├── staging/           # stg_ : typage, nettoyage, normalisation
│   └── model/             # dim_, fact_, agg_
└── data/                  # créé par le script, ignoré par git
    ├── raw/               # fichiers bruts téléchargés (cache)
    ├── lake/indispo/      # Parquet partitionné : date=AAAA-MM-JJ/part.parquet
    ├── netquery.duckdb    # base locale
    └── schema_profile.md  # colonnes, types, volumétrie, exemples
```

## Installation (Windows, PowerShell)

```powershell
git clone https://github.com/AmineBnh21/netquery.git
cd netquery
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Sous Linux ou macOS, activer l'environnement avec `source .venv/bin/activate`.

## Utilisation

```powershell
# 30 derniers jours de sites indisponibles
python scripts/ingest.py indispo --days 30

# une période précise
python scripts/ingest.py indispo --start 2026-10-01 --end 2026-10-31

# dernière archive des stations ANFR
python scripts/ingest.py anfr

# référentiel géographique (communes, départements, régions)
python scripts/ingest.py geo

# tout charger puis générer le profil du schéma
python scripts/ingest.py all --days 30

# régénérer seulement le profil
python scripts/ingest.py profile
```

Le script ne retélécharge jamais un fichier déjà présent dans `data/raw/`.
Au stade brut, toutes les colonnes sont en texte : le typage et le nettoyage
se font dans la couche staging.

Construire le modèle (staging, dimensions, faits, agrégats) puis lancer les contrôles qualité
(unicité des clés, intégrité des jointures, cohérence des agrégats) :

```powershell
python scripts/transform.py

# contrôles seulement
python scripts/transform.py --checks
```

Explorer la base :

```powershell
python -c "import duckdb; print(duckdb.connect('data/netquery.duckdb').sql('SHOW TABLES'))"
```

## Feuille de route

- [x] **1. Ingestion** : sites indisponibles (Parquet partitionné) et stations ANFR dans DuckDB, profil du schéma.
- [x] **2. Staging et modèle** : typage, normalisation des opérateurs, passage au format long (service, techno, état), tables `dim_`, `fact_`, `agg_`.
- [ ] **3. Schéma enrichi** : description de chaque table et colonne, valeurs possibles, clés de jointure, colonnes de partition, glossaire métier (YAML).
- [ ] **4. MVP text-to-SQL** : prompt + génération + exécution sur DuckDB, interface Streamlit.
- [ ] **5. Garde-fous** : validation sqlglot, lecture seule, `LIMIT`, timeout, boucle d'auto-correction.
- [ ] **6. RAG** : sélection des tables pertinentes, exemples few-shot dynamiques.
- [ ] **7. Cloud et coûts** : chargement dans BigQuery (partition par date, clustering par opérateur et département), dry run, seuil de coût, routage vers les tables `agg_`, cache du SQL normalisé, cache sémantique.
- [ ] **8. Évaluation** : 60 à 80 questions avec le SQL attendu ; comparaison version naïve / version optimisée.
- [ ] **9. Bonus** : questions de suivi, demande de clarification si la question est ambiguë, carte des résultats.

## Évaluation (prévue)

| Version | Exactitude (execution accuracy) | Octets scannés moyens | Coût moyen | Latence moyenne |
|---|---|---|---|---|
| Naïve : schéma complet, pas de cache | à mesurer | à mesurer | à mesurer | à mesurer |
| Optimisée : RAG, agrégats, partitions, caches | à mesurer | à mesurer | à mesurer | à mesurer |

## Exemples de questions

- Combien de sites SFR étaient en panne 4G dans le Rhône la semaine dernière ?
- Quel opérateur a eu le plus d'indisponibilités en Auvergne-Rhône-Alpes depuis le 1er octobre ?
- Combien de sites 5G ont été mis en service par opérateur chaque trimestre ?
- Quelles communes ont des sites hors service depuis plus de 7 jours ?
- Et pour Bouygues Telecom seulement ?

## Auteur

Mohamed Amine BENHAMAID, Master 2 Intelligence Artificielle, Université Claude Bernard Lyon 1
[GitHub](https://github.com/AmineBnh21) · [LinkedIn](https://www.linkedin.com/in/mohamed-amine-benhamaid)
