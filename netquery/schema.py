"""
Schéma enrichi : chargement des fichiers semantic/*.yaml, validation contre la base,
et rendu compact pour le prompt du LLM.

Usage :
  python -m netquery.schema --check                               # valider les YAML contre DuckDB
  python -m netquery.schema --render fact_indispo dim_departement # afficher le texte envoyé au LLM
  python -m netquery.schema --question "sites SFR en panne 4G dans le Rhône"
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[1]
SEMANTIC_DIR = ROOT / "semantic"
DB_PATH = ROOT / "data" / "netquery.duckdb"


# --------------------------------------------------------------------------- structures

@dataclass
class Column:
    name: str
    desc: str
    values: list | None = None


@dataclass
class Table:
    name: str
    description: str
    grain: str
    usage: str | None = None
    partition: str | None = None
    cluster: list[str] = field(default_factory=list)
    columns: list[Column] = field(default_factory=list)


@dataclass
class Term:
    nom: str
    mots: list[str]
    definition: str
    sql: str
    tables: list[str] = field(default_factory=list)


@dataclass
class Semantic:
    tables: dict[str, Table]
    joins: list[str]
    termes: list[Term]


def normalize(text: str) -> str:
    """Minuscules sans accents, pour comparer question et mots-clés."""
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


@lru_cache(maxsize=1)
def load(semantic_dir: Path = SEMANTIC_DIR) -> Semantic:
    schema = yaml.safe_load((semantic_dir / "schema.yaml").read_text(encoding="utf-8"))
    gloss = yaml.safe_load((semantic_dir / "glossaire.yaml").read_text(encoding="utf-8"))
    tables = {}
    for name, t in schema["tables"].items():
        cols = [Column(c, (spec or {}).get("desc", ""), (spec or {}).get("values"))
                for c, spec in t["columns"].items()]
        tables[name] = Table(name, t["description"], t["grain"], t.get("usage"),
                             t.get("partition"), t.get("cluster") or [], cols)
    termes = [Term(x["nom"], [normalize(str(m)) for m in x["mots"]], x["definition"].strip(), x["sql"].strip(),
                   x.get("tables") or [])
              for x in gloss["termes"]]
    return Semantic(tables, schema.get("joins", []), termes)


# --------------------------------------------------------------------------- rendu pour le prompt

def _fmt_values(values: list) -> str:
    vals = ["null" if v is None else (str(v) if isinstance(v, (int, float)) else f"'{v}'") for v in values]
    return " | ".join(vals)


def render_table(t: Table, types: dict[str, str] | None = None) -> str:
    """Une table au format pseudo-DDL commenté, compact pour économiser le contexte."""
    head = f"-- {t.description} Grain : {t.grain}."
    if t.usage:
        head += f"\n-- Usage : {t.usage}"
    if t.partition:
        head += f"\n-- Partition : {t.partition} (toujours filtrer sur {t.partition})"
    lines = [head, f"{t.name} ("]
    for i, c in enumerate(t.columns):
        typ = f" {types[c.name]}" if types and c.name in types else ""
        comment = c.desc + (f" ; valeurs : {_fmt_values(c.values)}" if c.values else "")
        sep = "," if i < len(t.columns) - 1 else ""
        lines.append(f"  {c.name}{typ}{sep}  -- {comment}")
    lines.append(")")
    return "\n".join(lines)


def render_schema(table_names: list[str] | None = None, with_types: bool = True) -> str:
    sem = load()
    names = table_names or list(sem.tables)
    unknown = [n for n in names if n not in sem.tables]
    if unknown:
        raise KeyError(f"tables inconnues du schéma enrichi : {unknown}")
    types = _db_types() if with_types else {}
    blocks = [render_table(sem.tables[n], types.get(n)) for n in names]
    joins = [j for j in sem.joins if any(_mentions(j, n) for n in names)]
    if joins:
        blocks.append("-- Jointures :\n" + "\n".join(f"--   {j}" for j in joins))
    return "\n\n".join(blocks)


def _mentions(join: str, table: str) -> bool:
    prefix = table.split("_")[0] + "_*"
    return table in join or prefix in join


def glossary_for(question: str) -> list[Term]:
    """Entrées du glossaire dont un mot-clé apparaît dans la question (mot entier, pluriel accepté)."""
    q = f" {re.sub(r'[^a-z0-9]+', ' ', normalize(question))} "
    hits = []
    for t in load().termes:
        patterns = [rf" {re.escape(re.sub(r'[^a-z0-9]+', ' ', m).strip())}[sx]? " for m in t.mots]
        if any(re.search(p, q) for p in patterns):
            hits.append(t)
    return hits


def render_glossary(terms: list[Term]) -> str:
    return "\n".join(f"- {t.nom} : {t.definition} SQL : {t.sql}" for t in terms)


# --------------------------------------------------------------------------- validation

def _db_types(db_path: Path = DB_PATH) -> dict[str, dict[str, str]]:
    if not db_path.exists():
        return {}
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute("""
            select table_name, column_name, data_type from information_schema.columns
            where table_schema = 'main' order by table_name, ordinal_position
        """).fetchall()
    finally:
        con.close()
    out: dict[str, dict[str, str]] = {}
    for t, c, typ in rows:
        out.setdefault(t, {})[c] = typ
    return out


def check(db_path: Path = DB_PATH) -> list[str]:
    """Renvoie la liste des erreurs (vide si tout est cohérent)."""
    sem = load()
    errors: list[str] = []
    types = _db_types(db_path)
    model_tables = {t for t in types if re.match(r"^(dim|fact|agg)_", t)}

    # 1. tables : documentées <-> présentes dans la base
    for t in sorted(model_tables - set(sem.tables)):
        errors.append(f"table {t} présente dans la base mais non documentée")
    for t in sorted(set(sem.tables) - set(types)):
        errors.append(f"table {t} documentée mais absente de la base")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        for t in sem.tables.values():
            if t.name not in types:
                continue
            db_cols, yml_cols = set(types[t.name]), {c.name for c in t.columns}
            # 2. colonnes : documentées <-> présentes
            errors += [f"{t.name}.{c} non documentée" for c in sorted(db_cols - yml_cols)]
            errors += [f"{t.name}.{c} documentée mais absente" for c in sorted(yml_cols - db_cols)]
            for c in t.columns:
                if not c.desc:
                    errors.append(f"{t.name}.{c.name} sans description")
            # 3. partition et clustering existent
            for c in ([t.partition] if t.partition else []) + t.cluster:
                if c not in db_cols:
                    errors.append(f"{t.name} : colonne de partition/cluster {c} absente")
            # 4. valeurs listées = valeurs réelles (si la liste se veut exhaustive)
            for c in t.columns:
                if not c.values or c.name not in db_cols or any(
                        isinstance(v, str) and v.startswith("autres") for v in c.values):
                    continue
                real = {r[0] for r in con.execute(f'select distinct "{c.name}" from "{t.name}"').fetchall()}
                listed = set(c.values)
                if real - listed:
                    errors.append(f"{t.name}.{c.name} : valeurs non listées {sorted(map(str, real - listed))}")
                if listed - real:
                    errors.append(f"{t.name}.{c.name} : valeurs listées absentes {sorted(map(str, listed - real))}")
    finally:
        con.close()

    # 5. le glossaire ne cite que des tables qui existent
    known = set(sem.tables)
    for term in sem.termes:
        refs = re.findall(r"\b((?:dim|fact|agg)_[a-z_]+)", term.sql + " " + term.definition) + term.tables
        for ref in refs:
            if ref not in known:
                errors.append(f"glossaire '{term.nom}' : table inconnue {ref}")
    return errors


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Schéma enrichi de NetQuery")
    p.add_argument("--check", action="store_true", help="valider les YAML contre la base")
    p.add_argument("--render", nargs="*", metavar="TABLE", help="afficher le schéma (toutes les tables si vide)")
    p.add_argument("--question", help="afficher les entrées du glossaire retenues pour une question")
    args = p.parse_args(argv)

    if args.check:
        errors = check()
        for e in errors:
            print(f"[ERR] {e}")
        print("schéma enrichi cohérent avec la base" if not errors else f"{len(errors)} erreur(s)")
        return 1 if errors else 0
    if args.render is not None:
        text = render_schema(args.render or None)
        print(text)
        print(f"\n({len(text)} caractères, environ {len(text) // 4} tokens)")
    if args.question:
        print(render_glossary(glossary_for(args.question)) or "(aucune entrée)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
