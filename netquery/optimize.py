"""
Réécritures de coût, appliquées au SQL validé avant exécution (indépendantes du LLM).

Dernier relevé en date explicite
  Le modèle écrit « hier » ou « actuellement » sous la forme `date = (select max(date) from fact_...)`.
  Sur BigQuery, un filtre de partition calculé par une sous-requête n'élimine aucune partition :
  mesuré sur « sites en panne hier dans le Rhône », 500 Ko lus contre 18 Ko avec la date en clair.
  La sous-requête est remplacée par la date du dernier relevé, lue une fois par session.
  Le résultat est identique (vérifié sur le jeu d'évaluation), seul le coût change.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache

import sqlglot
from sqlglot import exp

from netquery.sql_guard import DIALECT, CheckedSQL

# tables partitionnées par `date` dont le dernier relevé est le même
PARTITIONED = {"fact_indispo_site", "fact_indispo", "agg_sites_hs_dept_jour", "agg_indispo_dept_jour"}


@lru_cache(maxsize=1)
def latest_date() -> dt.date:
    """Date du dernier relevé (lue dans le petit agrégat journalier, quelques Ko)."""
    from netquery.sql_guard import readonly_connection

    con = readonly_connection()
    try:
        return con.execute("select max(date) from agg_sites_hs_dept_jour").fetchone()[0]
    finally:
        con.close()


def _is_latest_date_subquery(node: exp.Expression) -> bool:
    """(select max(date) from <table partitionnée>), sans filtre ni regroupement."""
    if not isinstance(node, exp.Subquery) or not isinstance(node.this, exp.Select):
        return False
    sel = node.this
    if len(sel.expressions) != 1 or any(sel.args.get(k) for k in ("where", "group", "having", "joins")):
        return False
    agg = sel.expressions[0].unalias()
    if not isinstance(agg, exp.Max) or not isinstance(agg.this, exp.Column) or agg.this.name != "date":
        return False
    tables = list(sel.find_all(exp.Table))
    return len(tables) == 1 and tables[0].name in PARTITIONED


def rewrite_latest_date(checked: CheckedSQL, latest: dt.date | None = None) -> tuple[CheckedSQL, int]:
    """Remplace les sous-requêtes « dernier relevé » par une date explicite.
    Renvoie le SQL réécrit et le nombre de remplacements."""
    tree = sqlglot.parse_one(checked.sql, read=DIALECT)
    targets = [n for n in tree.find_all(exp.Subquery) if _is_latest_date_subquery(n)]
    if not targets:
        return checked, 0
    day = (latest or latest_date()).isoformat()
    for node in targets:
        node.replace(exp.cast(exp.Literal.string(day), "date"))
    new = CheckedSQL(sql=tree.sql(dialect=DIALECT, pretty=True), tables=checked.tables,
                     limit_added=checked.limit_added)
    return new, len(targets)
