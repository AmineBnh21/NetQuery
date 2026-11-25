"""
Chargement du modèle (dim_, fact_, agg_) de DuckDB vers BigQuery.

Chaque table est exportée en Parquet (data/export/), puis chargée par un job de chargement
(gratuit, y compris dans le bac à sable BigQuery). Les tables d'indisponibilité sont partitionnées
par jour sur `date` et regroupées (clustering) par opérateur et département : une requête filtrée
sur une période et un département ne lit que les blocs utiles.

Bac à sable BigQuery (sans facturation) : les partitions expirent 60 jours après leur date et les
tables 60 jours après leur création ; relancer ce script pour recharger.

Prérequis : gcloud auth application-default login ; GCP_PROJECT dans .env

Usage :
  python scripts/load_bigquery.py              # toutes les tables
  python scripts/load_bigquery.py fact_indispo # une table
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import duckdb
from dotenv import load_dotenv
from google.cloud import bigquery

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "netquery.duckdb"
EXPORT_DIR = ROOT / "data" / "export"

load_dotenv(ROOT / ".env")
PROJECT = os.getenv("GCP_PROJECT")
DATASET = os.getenv("BQ_DATASET", "netquery")
LOCATION = os.getenv("BQ_LOCATION", "EU")

# table -> (colonne de partition journalière, colonnes de clustering)
LAYOUT: dict[str, tuple[str | None, list[str]]] = {
    "fact_indispo": ("date", ["operateur_id", "departement", "techno"]),
    "fact_indispo_site": ("date", ["operateur_id", "departement"]),
    "agg_indispo_dept_jour": ("date", ["operateur_id", "departement"]),
    "agg_sites_hs_dept_jour": ("date", ["operateur_id", "departement"]),
    # pas de partition par date de mise en service : le bac à sable supprimerait les partitions anciennes
    "fact_station": (None, ["operateur_id", "techno", "departement"]),
    "dim_site": (None, ["operateur_id", "departement"]),
    "dim_commune": (None, ["departement"]),
    "dim_departement": (None, []),
    "dim_operateur": (None, []),
    "dim_date": (None, []),
}

log = logging.getLogger("load_bigquery")


def export_parquet(con: duckdb.DuckDBPyConnection, table: str) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORT_DIR / f"{table}.parquet"
    con.execute(f"copy (select * from {table}) to '{path.as_posix()}' (format parquet, compression zstd)")
    return path


def load_table(client: bigquery.Client, table: str, path: Path) -> bigquery.Table:
    partition, cluster = LAYOUT[table]
    cfg = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        parquet_options=bigquery.ParquetOptions.from_api_repr({"enableListInference": True}),
    )
    if partition:
        cfg.time_partitioning = bigquery.TimePartitioning(type_=bigquery.TimePartitioningType.DAY, field=partition)
    if cluster:
        cfg.clustering_fields = cluster
    table_id = f"{PROJECT}.{DATASET}.{table}"
    # WRITE_TRUNCATE ne change pas le partitionnement d'une table existante : on la recrée
    client.delete_table(table_id, not_found_ok=True)
    with open(path, "rb") as f:
        job = client.load_table_from_file(f, table_id, job_config=cfg, location=LOCATION)
    job.result()
    return client.get_table(table_id)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if not PROJECT:
        log.error("GCP_PROJECT absent du fichier .env")
        return 1
    tables = (argv if argv is not None else sys.argv[1:]) or list(LAYOUT)
    unknown = [t for t in tables if t not in LAYOUT]
    if unknown:
        log.error("tables inconnues : %s", unknown)
        return 1

    client = bigquery.Client(project=PROJECT, location=LOCATION)
    ds = bigquery.Dataset(f"{PROJECT}.{DATASET}")
    ds.location = LOCATION
    ds.description = "NetQuery : modèle des données réseau mobile (Arcep, ANFR)"
    client.create_dataset(ds, exists_ok=True)

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        for t in tables:
            t0 = time.perf_counter()
            path = export_parquet(con, t)
            bq_table = load_table(client, t, path)
            part, clus = LAYOUT[t]
            log.info("%-24s %9s lignes %7.1f Mo  partition=%s cluster=%s  (%.0f s)",
                     t, f"{bq_table.num_rows:,}".replace(",", " "), bq_table.num_bytes / 1e6,
                     part or "-", ",".join(clus) or "-", time.perf_counter() - t0)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
