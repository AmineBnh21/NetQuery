"""
Exécution sur BigQuery : traduction DuckDB -> BigQuery, estimation du coût avant exécution, garde-fous.

Le LLM écrit du SQL DuckDB (validé par sql_guard). Pour BigQuery :
  1. traduction du dialecte avec sqlglot (FILTER -> COUNTIF, intervalles, fonctions de date...) ;
  2. dry run : BigQuery renvoie le nombre d'octets qui seraient lus, sans rien exécuter ni facturer ;
  3. refus si l'estimation dépasse BQ_MAX_BYTES ; sinon exécution avec maximum_bytes_billed
     (BigQuery annule lui-même un job qui dépasserait) et un délai maximal.

Les tables sont référencées sans préfixe : le jeu de données par défaut est BQ_DATASET.
Tarif à la demande utilisé pour l'estimation : BQ_PRICE_PER_TIB (6,25 USD par Tio par défaut),
avec le minimum facturé de 10 Mo par table lue.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from functools import lru_cache

import pandas as pd
import sqlglot
from dotenv import load_dotenv

from netquery.sql_guard import CheckedSQL

load_dotenv()

PROJECT = os.getenv("GCP_PROJECT")
DATASET = os.getenv("BQ_DATASET", "netquery")
LOCATION = os.getenv("BQ_LOCATION", "EU")
MAX_BYTES = int(float(os.getenv("BQ_MAX_BYTES", str(1e9))))   # 1 Go par requête au maximum
PRICE_PER_TIB = float(os.getenv("BQ_PRICE_PER_TIB", "6.25"))
MIN_BYTES_PER_TABLE = 10 * 1024 ** 2
TIMEOUT_S = float(os.getenv("SQL_TIMEOUT_S", "30"))


class CostLimitError(RuntimeError):
    """Requête refusée car trop coûteuse."""


@dataclass
class Estimate:
    sql: str                 # SQL BigQuery
    bytes_processed: int     # octets lus estimés par le dry run
    bytes_billed: int        # avec le minimum de 10 Mo par table
    cost_usd: float
    tables: list[str]


def to_bigquery(sql_duckdb: str) -> str:
    """Traduit le SQL DuckDB du modèle en SQL BigQuery."""
    tree = sqlglot.parse_one(sql_duckdb, read="duckdb")
    sql = tree.sql(dialect="bigquery", pretty=True)
    # Piège de sens : en BigQuery, WEEK commence le dimanche ; en DuckDB, le lundi (semaine ISO).
    # sqlglot traduit date_trunc('week', current_date) en TIMESTAMP_TRUNC : on revient à une DATE.
    return re.sub(r"(?:DATE|TIMESTAMP)_TRUNC\(([^,()]+),\s*WEEK\)", r"DATE_TRUNC(\1, ISOWEEK)", sql)


@lru_cache(maxsize=1)
def client():
    from google.cloud import bigquery

    if not PROJECT:
        raise RuntimeError("GCP_PROJECT absent du fichier .env")
    return bigquery.Client(project=PROJECT, location=LOCATION)


def _job_config(dry_run: bool, max_bytes: int | None = None):
    from google.cloud import bigquery

    cfg = bigquery.QueryJobConfig(
        dry_run=dry_run,
        use_query_cache=not dry_run,
        default_dataset=f"{PROJECT}.{DATASET}",
        job_timeout_ms=int(TIMEOUT_S * 1000),
    )
    if max_bytes is not None:
        cfg.maximum_bytes_billed = max_bytes
    return cfg


def estimate(checked: CheckedSQL) -> Estimate:
    sql = to_bigquery(checked.sql)
    job = client().query(sql, job_config=_job_config(dry_run=True))
    processed = int(job.total_bytes_processed or 0)
    billed = max(processed, MIN_BYTES_PER_TABLE * max(len(checked.tables), 1))
    return Estimate(sql, processed, billed, billed / 1024 ** 4 * PRICE_PER_TIB, checked.tables)


@dataclass
class BQResult:
    df: pd.DataFrame
    estimate: Estimate
    elapsed_s: float
    cache_hit: bool


def execute(checked: CheckedSQL, max_bytes: int = MAX_BYTES) -> BQResult:
    est = estimate(checked)
    if est.bytes_processed > max_bytes:
        raise CostLimitError(f"requête estimée à {est.bytes_processed / 1e6:.0f} Mo, "
                             f"au-delà du seuil de {max_bytes / 1e6:.0f} Mo")
    t0 = time.perf_counter()
    job = client().query(est.sql, job_config=_job_config(dry_run=False, max_bytes=max_bytes))
    df = job.result(timeout=TIMEOUT_S).to_dataframe(create_bqstorage_client=False)
    return BQResult(df, est, time.perf_counter() - t0, bool(job.cache_hit))
