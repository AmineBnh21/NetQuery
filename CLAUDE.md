# NetQuery : contexte pour Claude Code

Assistant text-to-SQL en français sur les données publiques des réseaux mobiles
(Arcep sites indisponibles, ANFR stations, Arcep Mon Réseau Mobile).
Projet portfolio d'un étudiant en M2 IA, orienté poste de Data Analyst / ingénieur IA chez un opérateur télécom.

## Environnement

- Développement sous Windows (PowerShell). Donner les commandes PowerShell en priorité.
- Python 3.11+, environnement virtuel `.venv`.
- Base locale : DuckDB (`data/netquery.duckdb`). Cible cloud plus tard : BigQuery.

## État actuel

- `scripts/ingest.py` fonctionne : sites indisponibles en Parquet partitionné (`data/lake/indispo/date=.../`),
  vue `raw_sites_indispo`, tables `raw_anfr_*`, référentiel `raw_geo_*` (geo.api.gouv.fr), profil dans `data/schema_profile.md`.
- Étape 2 faite : `scripts/transform.py` exécute `sql/staging/*.sql` puis `sql/model/*.sql` et lance des contrôles qualité.
  Tables : `stg_*`, `dim_operateur`, `dim_departement`, `dim_commune`, `dim_date`, `dim_site`,
  `fact_station`, `fact_indispo_site`, `fact_indispo` (format long), `agg_indispo_dept_jour`, `agg_sites_hs_dept_jour`.
- `raw_anfr_stations` est une vue vers la table ANFR la plus récente (son nom contient la date de l'archive).
- Pièges connus : 5G ANFR toujours « Techniquement opérationnel » (utiliser `est_actif`) ;
  `fact_indispo` non additif entre technos.
- Étape 3 faite : `semantic/schema.yaml` (tables, colonnes, valeurs, jointures, partitions) et
  `semantic/glossaire.yaml` (mots-clés -> définition + indice SQL). `netquery/schema.py` : `render_schema(tables)`,
  `glossary_for(question)`, `check()`. Après toute modification du modèle SQL : `python -m netquery.schema --check`.
- Prochaine étape : MVP text-to-SQL (étape 4 du README), avec `netquery/llm.py` générique (voir section LLM).

## Conventions

- Code et commentaires en français, noms de variables en anglais ou français sans accents.
- Tables : `raw_` (brut), `stg_` (typé, nettoyé), `dim_` / `fact_` (modèle), `agg_` (agrégats pour réduire les coûts).
- SQL en minuscules pour les mots-clés n'est pas imposé, mais rester cohérent dans un même fichier.
- Pas de tirets longs dans les textes destinés au README ou au CV.

## Règles de sécurité du text-to-SQL (à respecter dans tout le code)

- Seules les requêtes `SELECT` (et `WITH ... SELECT`) sont autorisées, vérifiées avec `sqlglot`.
- Connexion en lecture seule pour l'exécution des requêtes générées.
- `LIMIT` ajouté automatiquement, timeout, et estimation du coût avant exécution sur BigQuery.
- Aucune clé d'API dans le code : variables d'environnement via `.env` (ignoré par git).

## Ne pas faire

- Ne pas committer `data/` ni de fichiers `.duckdb`.
- Ne pas retélécharger des fichiers déjà en cache dans `data/raw/`.
## LLM

- Fournisseur : Ollama en local (http://localhost:11434/v1, API compatible OpenAI, SDK `openai`).
- Modèle par défaut : qwen2.5-coder:3b (machine à 8 Go de RAM), temperature=0, num_ctx=4096.
- Configuration via .env : LLM_BASE_URL, LLM_MODEL.
- Garder llm.py générique pour pouvoir changer de modèle sans modifier le reste du code.
- Envoyer un schéma court (tables utiles uniquement) : le contexte et la RAM sont limités.