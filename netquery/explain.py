"""
Transparence et clarification.

- describe_filters : les filtres de la requête, en français (« opérateur : SFR », « date : 24/11/2026 »),
  pour que l'utilisateur vérifie ce qui a été compris sans lire le SQL.
- unrequested_filters : filtres que la question ne demande pas. Première cause d'échec mesurée à
  l'évaluation : le petit modèle recopie un filtre d'un exemple (« SFR », « 5G », « 4 opérateurs »).
  On le signale au lieu de le corriger en silence (une correction automatique devrait d'abord être
  validée sur un nouveau jeu de questions).
- clarification : question trop vague pour être traduite (aucune notion connue) ; on demande une
  précision plutôt que de laisser le modèle inventer.
"""

from __future__ import annotations

import datetime as dt
import re

import sqlglot
from sqlglot import exp

from netquery.schema import glossary_for, normalize

OPERATOR_NAMES = {
    "ORANGE": ["orange"], "SFR": ["sfr"], "BOUYGUES": ["bouygues", "bytel"], "FREE": ["free"],
    "FREE_CARAIBES": ["free"],
}
LABELS = {
    "operateur_id": "opérateur", "departement": "département", "techno": "technologie",
    "service": "service", "raison": "cause", "bande_mhz": "bande (MHz)", "statut": "statut ANFR",
    "region": "région", "nom": "nom", "date": "date", "département": "département", "commune": "commune", "annee": "année", "nb_jours_depuis_debut": "jours de panne", "population": "population", "hauteur_m": "hauteur (m)", "est_hs": "hors service", "trimestre": "trimestre",
}


TABLE_LABELS = {"dim_departement": {"nom": "département", "region": "région"},
                "dim_commune": {"nom": "commune", "region": "région"},
                "dim_operateur": {"nom": "opérateur"}}


def _literal(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Literal):
        return node.this
    if isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal):
        return node.this.this
    return _evaluate_constant(node)


def _evaluate_constant(node: exp.Expression) -> str | None:
    """Valeur d'une expression constante (ex. date_trunc('week', current_date) - interval 7 day),
    calculée par DuckDB. Uniquement sans colonne ni sous-requête : rien n'est lu dans les tables."""
    if node.find(exp.Column, exp.Subquery, exp.Select, exp.Table):
        return None
    if not node.find(exp.CurrentDate, exp.CurrentTimestamp, exp.Interval, exp.DateTrunc, exp.TimestampTrunc):
        return None
    from netquery.sql_guard import readonly_connection

    try:
        con = readonly_connection()
        try:
            value = con.execute(f"select cast(({node.sql(dialect='duckdb')}) as date)").fetchone()[0]
        finally:
            con.close()
        return value.isoformat() if value else None
    except Exception:  # noqa: BLE001 - l'explication est un bonus
        return None


def _fmt(col: str, value: str) -> str:
    if col == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return dt.date.fromisoformat(value).strftime("%d/%m/%Y")
    return value


def extract_filters(sql: str) -> list[tuple[str, str, list[str]]]:
    """(colonne, opérateur, valeurs) des conditions simples colonne/littéral de la requête."""
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.SqlglotError:
        return []
    aliases = {t.alias_or_name: t.name for t in tree.find_all(exp.Table)}
    out = []
    for node in tree.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Between):
        col = node.this if isinstance(node.this, exp.Column) else None
        if col is None:
            continue
        table = aliases.get(col.table) if col.table else None
        name = TABLE_LABELS.get(table, {}).get(col.name, col.name) if table else col.name
        if isinstance(node, exp.In):
            vals = [v for v in (_literal(e) for e in node.expressions) if v is not None]
            if vals:
                out.append((name, "parmi", vals))
        elif isinstance(node, exp.Between):
            lo, hi = _literal(node.args["low"]), _literal(node.args["high"])
            if lo and hi:
                out.append((name, "entre", [lo, hi]))
        else:
            val = _literal(node.expression)
            if val is not None:
                sym = {exp.EQ: "=", exp.GT: ">", exp.GTE: "≥", exp.LT: "<", exp.LTE: "≤"}[type(node)]
                out.append((name, sym, [val]))
    return out


def describe_filters(sql: str) -> list[str]:
    lines = []
    for col, op, vals in extract_filters(sql):
        label = LABELS.get(col, col)
        vals_fmt = [_fmt(col, v) for v in vals]
        if op == "=":
            lines.append(f"{label} : {vals_fmt[0]}")
        elif op == "parmi":
            lines.append(f"{label} : {', '.join(vals_fmt)}")
        elif op == "entre":
            lines.append(f"{label} : du {vals_fmt[0]} au {vals_fmt[1]}")
        else:
            lines.append(f"{label} {op} {vals_fmt[0]}")
    return list(dict.fromkeys(lines))


def unrequested_filters(question: str, sql: str, context: str = "") -> list[str]:
    """Filtres sur l'opérateur ou la technologie que ni la question ni le contexte ne mentionnent."""
    from netquery.prompt import resolve_places

    text = normalize(f"{context} {question}")
    words = set(re.findall(r"[a-z0-9]+", text))
    places = {p.code for p in resolve_places(f"{context} {question}")}
    warnings = []
    for col, _op, vals in extract_filters(sql):
        if col == "operateur_id" and set(vals) == {"ORANGE", "SFR", "BOUYGUES", "FREE"}:
            if not re.search(r"\bmetropol", text):
                warnings.append("limité aux 4 opérateurs de métropole (les opérateurs d'outre-mer sont exclus)")
            continue
        for v in vals:
            if col == "operateur_id" and v in OPERATOR_NAMES and not words & set(OPERATOR_NAMES[v]) \
                    and not re.search(r"\bmetropol", text):
                warnings.append(f"opérateur {v}")
            elif col == "techno" and v.lower() not in words and not _techno_synonym(v, words):
                warnings.append(f"technologie {v}")
            elif col == "departement" and v not in places and v not in words:
                warnings.append(f"département {v}")
    return list(dict.fromkeys(warnings))


def _techno_synonym(techno: str, words: set[str]) -> bool:
    syn = {"2G": {"gsm"}, "3G": {"umts"}, "4G": {"lte"}, "5G": {"nr"}}
    return bool(syn.get(techno.upper(), set()) & words)


def clarification(question: str) -> str | None:
    """Message de demande de précision, ou None si la question est exploitable."""
    from netquery.prompt import resolve_places

    if glossary_for(question) or resolve_places(question):
        return None
    words = re.findall(r"[a-z0-9]+", normalize(question))
    if len(words) >= 4 and any(w in words for w in ("site", "sites", "emetteur", "emetteurs", "antenne", "antennes")):
        return None
    return ("Je n'ai pas reconnu de notion du domaine dans cette question. Précisez par exemple : "
            "des **pannes** (« sites en panne hier dans le Rhône »), le **déploiement** "
            "(« émetteurs 5G actifs par opérateur ») ou le **parc** (« nombre de sites Orange en Bretagne »).")
