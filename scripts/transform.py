"""
Transformation : couche brute (raw_) -> staging (stg_) -> modèle (dim_, fact_, agg_) dans DuckDB.

Les requêtes sont dans sql/staging/ puis sql/model/, exécutées par ordre alphabétique
(préfixes 00_, 10_, ...). Des contrôles qualité sont lancés à la fin : le script renvoie
un code d'erreur si l'un d'eux échoue.

Prérequis : python scripts/ingest.py all --days 30

Usage :
  python scripts/transform.py            # transformation + contrôles
  python scripts/transform.py --checks   # contrôles seulement
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "netquery.duckdb"
SQL_DIRS = [ROOT / "sql" / "staging", ROOT / "sql" / "model"]

log = logging.getLogger("transform")


# --------------------------------------------------------------------------- transformation

def link_latest_anfr(con: duckdb.DuckDBPyConnection) -> None:
    """Le nom de la table ANFR dépend de la date de l'archive (raw_anfr_observatoireod_AAAAMMJJ).
    On crée une vue stable raw_anfr_stations vers la plus récente."""
    tables = [t for (t,) in con.execute("""
        select table_name from information_schema.tables
        where table_schema = 'main' and table_type = 'BASE TABLE'
          and table_name like 'raw_anfr_observatoire%'
        order by table_name desc
    """).fetchall()]
    if not tables:
        raise SystemExit("aucune table raw_anfr_observatoire* : lancer d'abord scripts/ingest.py anfr")
    con.execute(f'create or replace view raw_anfr_stations as select * from "{tables[0]}"')
    log.info("raw_anfr_stations -> %s", tables[0])


def run_sql_files(con: duckdb.DuckDBPyConnection) -> None:
    for folder in SQL_DIRS:
        for f in sorted(folder.glob("*.sql")):
            t0 = time.perf_counter()
            con.execute(f.read_text(encoding="utf-8"))
            log.info("%-40s %.1f s", f"{folder.name}/{f.name}", time.perf_counter() - t0)


def summary(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute("""
        select table_name, estimated_size
        from duckdb_tables()
        where schema_name = 'main' and regexp_matches(table_name, '^(stg|dim|fact|agg)_')
        order by table_name
    """).fetchall()
    for name, _ in rows:
        n = con.execute(f'select count(*) from "{name}"').fetchone()[0]
        log.info("  %-28s %10s lignes", name, f"{n:,}".replace(",", " "))


# --------------------------------------------------------------------------- contrôles qualité

# (description, requête qui renvoie le nombre de lignes en défaut)
CHECKS_ZERO = [
    ("dim_operateur : clé unique",
     "select count(*) - count(distinct operateur_id) from dim_operateur"),
    ("dim_departement : clé unique",
     "select count(*) - count(distinct departement) from dim_departement"),
    ("dim_commune : clé unique",
     "select count(*) - count(distinct code_insee) from dim_commune"),
    ("dim_date : clé unique",
     "select count(*) - count(distinct date) from dim_date"),
    ("dim_site : clé unique",
     "select count(*) - count(distinct site_id) from dim_site"),
    ("fact_station : clé unique",
     "select count(*) - count(distinct emetteur_id) from fact_station"),
    ("fact_indispo_site : grain (date, site_id) unique",
     "select count(*) - count(distinct (date, site_id)) from fact_indispo_site"),
    ("fact_indispo : grain (date, site_id, service, techno) unique",
     "select count(*) - count(distinct (date, site_id, service, techno)) from fact_indispo"),
    ("fact_indispo_site : site présent dans dim_site",
     "select count(*) from fact_indispo_site f anti join dim_site s using (site_id)"),
    ("fact_station : site présent dans dim_site",
     "select count(*) from fact_station f anti join dim_site s using (site_id)"),
    ("faits : opérateur présent dans dim_operateur",
     """select count(*) from (
            select operateur_id from fact_indispo_site union all select operateur_id from fact_station
        ) f anti join dim_operateur o using (operateur_id)"""),
    ("faits : date présente dans dim_date",
     "select count(*) from fact_indispo_site f anti join dim_date d using (date)"),
    ("fact_indispo_site : code INSEE présent dans dim_commune",
     "select count(*) from fact_indispo_site f anti join dim_commune c using (code_insee) where f.code_insee is not null"),
    ("fact_indispo : service et techno valides",
     """select count(*) from fact_indispo
        where service not in ('voix', 'data') or techno not in ('2G', '3G', '4G', '5G')"""),
    ("staging : aucune ligne Arcep perdue hors doublons",
     """select (select count(distinct (date, coalesce(nullif(trim(station_anfr), ''),
                                                       concat_ws('|', operateur, code_insee, lat, lon))))
               from raw_sites_indispo)
             - (select count(*) from fact_indispo_site)"""),
    ("agrégat : total cohérent avec fact_indispo_site",
     "select (select sum(nb_sites_indispo) from agg_sites_hs_dept_jour) - (select count(*) from fact_indispo_site)"),
    ("dim_site : 4 opérateurs de métropole",
     "select 4 - count(distinct operateur_id) from dim_site where operateur_id in ('ORANGE','SFR','BOUYGUES','FREE')"),
]

# (description, requête qui renvoie un taux entre 0 et 1, seuil minimal)
CHECKS_RATIO = [
    ("fact_indispo_site : site retrouvé dans l'ANFR",
     "select avg((s.source = 'anfr')::int) from fact_indispo_site f join dim_site s using (site_id)", 0.90),
    ("fact_indispo_site : département connu du référentiel",
     "select avg((d.nom is not null)::int) from fact_indispo_site f left join dim_departement d using (departement)", 0.99),
    ("dim_site (ANFR) : coordonnées en France (lat/lon renseignées)",
     "select avg((lat is not null and lon is not null)::int) from dim_site where source = 'anfr'", 0.99),
    ("fact_indispo_site : date de début renseignée",
     "select avg((debut is not null)::int) from fact_indispo_site", 0.85),
]


def run_checks(con: duckdb.DuckDBPyConnection) -> bool:
    ok = True
    for desc, sql in CHECKS_ZERO:
        n = con.execute(sql).fetchone()[0] or 0
        status = "OK " if n == 0 else "ERR"
        ok &= n == 0
        log.info("[%s] %s%s", status, desc, "" if n == 0 else f" ({n} lignes en défaut)")
    for desc, sql, seuil in CHECKS_RATIO:
        r = con.execute(sql).fetchone()[0] or 0.0
        status = "OK " if r >= seuil else "ERR"
        ok &= r >= seuil
        log.info("[%s] %s : %.2f %% (seuil %.0f %%)", status, desc, 100 * r, 100 * seuil)
    return ok


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Transformation raw -> staging -> modèle")
    p.add_argument("--checks", action="store_true", help="lancer seulement les contrôles qualité")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    if not DB_PATH.exists():
        log.error("base absente (%s) : lancer d'abord scripts/ingest.py all", DB_PATH)
        return 1

    con = duckdb.connect(str(DB_PATH))
    try:
        if not args.checks:
            link_latest_anfr(con)
            run_sql_files(con)
            summary(con)
        log.info("contrôles qualité :")
        ok = run_checks(con)
    finally:
        con.close()
    if not ok:
        log.error("des contrôles qualité ont échoué")
        return 1
    log.info("tous les contrôles sont passés")
    return 0


if __name__ == "__main__":
    sys.exit(main())
