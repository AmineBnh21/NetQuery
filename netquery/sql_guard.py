"""
Garde-fous du SQL généré : validation avec sqlglot puis exécution encadrée.

Validation (avant toute exécution) :
  - une seule instruction, de type SELECT (WITH ... SELECT et UNION autorisés) ;
  - uniquement des tables du modèle autorisées (pas de table brute, pas de fonction de lecture
    de fichiers comme read_csv, pas de table système) ;
  - LIMIT ajouté ou abaissé au maximum autorisé.

Exécution :
  - connexion DuckDB en lecture seule, accès aux fichiers externes désactivé, configuration verrouillée ;
  - interruption de la requête au-delà du délai maximal.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd
import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "netquery.duckdb"

MAX_ROWS = int(os.getenv("SQL_MAX_ROWS", "1000"))
TIMEOUT_S = float(os.getenv("SQL_TIMEOUT_S", "10"))
DIALECT = "duckdb"


class UnsafeSQLError(ValueError):
    """Requête refusée par les garde-fous."""


@dataclass
class CheckedSQL:
    sql: str              # SQL validé, avec LIMIT, prêt à exécuter
    tables: list[str]     # tables du modèle utilisées
    limit_added: bool


def allowed_tables_default() -> set[str]:
    from netquery.schema import load
    return set(load().tables)


def validate(sql: str, allowed_tables: set[str] | None = None, max_rows: int = MAX_ROWS) -> CheckedSQL:
    allowed = allowed_tables if allowed_tables is not None else allowed_tables_default()
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise UnsafeSQLError("requête vide")

    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except sqlglot.errors.SqlglotError as e:  # ParseError, TokenError...
        raise UnsafeSQLError(f"SQL invalide : {e}") from e
    if len(statements) != 1:
        raise UnsafeSQLError("une seule instruction est autorisée")
    tree = statements[0]

    # 1. uniquement une lecture (SELECT, éventuellement WITH et UNION / INTERSECT / EXCEPT)
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise UnsafeSQLError(f"seules les requêtes SELECT sont autorisées (reçu : {tree.key.upper()})")
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command,
                 exp.Merge, exp.Copy, exp.Pragma, exp.Set, exp.Use, exp.Into)
    for node in tree.walk():
        if isinstance(node, forbidden):
            raise UnsafeSQLError(f"instruction interdite : {node.key.upper()}")

    # 2. tables : seulement celles du modèle (les CTE définies dans la requête sont acceptées)
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    used: set[str] = set()
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            # read_csv('...'), read_parquet(...), range(...), etc.
            raise UnsafeSQLError(f"fonction de table interdite : {table.this.sql(DIALECT)}")
        name = table.name.lower()
        if table.args.get("db") or table.args.get("catalog"):
            raise UnsafeSQLError(f"table qualifiée interdite : {table.sql(DIALECT)}")
        if name in cte_names:
            continue
        if name not in allowed:
            raise UnsafeSQLError(f"table non autorisée : {name}")
        used.add(name)

    # 3. LIMIT : ajouté s'il manque, abaissé s'il dépasse le maximum
    limit_added = False
    limit = tree.args.get("limit")
    current = None
    if limit is not None:
        value = limit.expression if isinstance(limit, exp.Limit) else limit
        try:
            current = int(value.this) if isinstance(value, exp.Literal) else None
        except (TypeError, ValueError):
            current = None
    if current is None or current > max_rows:
        tree = tree.limit(max_rows, copy=False) if isinstance(tree, exp.Select) else \
            exp.select("*").from_(tree.subquery("q")).limit(max_rows)
        limit_added = True

    return CheckedSQL(sql=tree.sql(dialect=DIALECT, pretty=True), tables=sorted(used), limit_added=limit_added)


# --------------------------------------------------------------------------- exécution

def readonly_connection(db_path: Path = DB_PATH) -> duckdb.DuckDBPyConnection:
    # configuration verrouillée dès l'ouverture : une connexion suivante au même fichier
    # partage l'instance DuckDB et doit déclarer exactement la même configuration
    return duckdb.connect(str(db_path), read_only=True,
                          config={"enable_external_access": False, "lock_configuration": True})


@dataclass
class ExecResult:
    df: pd.DataFrame
    elapsed_s: float
    truncated: bool


def execute(checked: CheckedSQL, con: duckdb.DuckDBPyConnection | None = None,
            timeout_s: float = TIMEOUT_S, max_rows: int = MAX_ROWS) -> ExecResult:
    own = con is None
    con = con or readonly_connection()
    timer = threading.Timer(timeout_s, con.interrupt)
    t0 = time.perf_counter()
    timer.start()
    try:
        df = con.execute(checked.sql).df()
    except duckdb.InterruptException as e:
        raise TimeoutError(f"requête interrompue après {timeout_s:.0f} s") from e
    finally:
        timer.cancel()
        if own:
            con.close()
    return ExecResult(df=df, elapsed_s=time.perf_counter() - t0, truncated=len(df) >= max_rows)
