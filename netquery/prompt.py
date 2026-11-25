"""
Construction du prompt : choix des tables utiles, glossaire, exemples, consignes.

Le contexte du modèle local est petit (4096 tokens) : on n'envoie que les tables utiles
à la question, les entrées du glossaire qui la concernent et deux exemples au plus.

Usage :
  python -m netquery.prompt "Combien de sites SFR en panne 4G dans le Rhône ?"   # afficher le prompt
  python -m netquery.prompt --check                                              # valider les exemples
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from netquery.schema import SEMANTIC_DIR, Term, glossary_for, normalize, render_glossary, render_schema

FACTS_INDISPO = ("fact_indispo_site", "fact_indispo")
DEFAULT_FACT = "fact_indispo_site"

SYSTEM = """Tu traduis des questions en français en une requête SQL DuckDB.
Règles :
- Une seule requête SELECT (WITH autorisé). Jamais de INSERT, UPDATE, DELETE, CREATE.
- Utilise uniquement les tables et colonnes listées. Respecte les valeurs exactes indiquées (ex. 'SFR', '4G').
- Pour compter des sites sur plusieurs jours ou plusieurs technos : count(distinct site_id).
- Si la question décrit l'état actuel (« sont », « ont », « actuellement », « depuis plus de N jours »)
  sans donner de période, filtre sur le dernier relevé : date = (select max(date) from fact_indispo_site).
- Fais tous les regroupements demandés (« par opérateur », « par trimestre »...) : une colonne et un GROUP BY pour chacun.
- Utilise les correspondances de lieux fournies telles quelles (codes département).
- Donne des noms de colonnes explicites en minuscules (ex. nb_sites).
- Réponds uniquement par la requête dans un bloc ```sql```, sans explication."""


@dataclass
class Example:
    question: str
    tables: list[str]
    sql: str


@lru_cache(maxsize=1)
def load_examples(path: Path = SEMANTIC_DIR / "exemples.yaml") -> list[Example]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Example(e["question"], e["tables"], e["sql"].strip()) for e in data["exemples"]]


# --------------------------------------------------------------------------- lieux cités dans la question

# Noms de départements qui sont aussi des mots courants : reconnus seulement avec une majuscule
# (« dans la Somme » oui, « la somme des sites » non).
AMBIGUOUS_PLACES = {"somme", "lot", "manche", "cher", "nord", "var", "ain", "aube", "orne", "eure",
                    "gard", "loire", "vienne", "indre", "oise", "marne", "gers", "aude", "tarn"}


@dataclass
class Place:
    kind: str      # departement ou region
    name: str
    code: str      # code département, ou nom exact de la région

    def hint(self) -> str:
        if self.kind == "departement":
            return f"{self.name} = département '{self.code}' (colonne departement = '{self.code}')"
        return f"{self.name} = région (dim_departement.region = '{self.code}')"


def _key(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", normalize(text)).strip() + " "


@lru_cache(maxsize=1)
def _place_index() -> list[tuple[str, Place]]:
    from netquery.sql_guard import readonly_connection

    try:
        con = readonly_connection()
        rows = con.execute("select departement, nom, region from dim_departement where nom is not null").fetchall()
        con.close()
    except Exception:  # base absente
        return []
    index = [(_key(nom), Place("departement", nom, dep)) for dep, nom, _ in rows]
    index += [(_key(reg), Place("region", reg, reg)) for reg in {r for _, _, r in rows if r}]
    # les noms les plus longs d'abord : « Auvergne-Rhône-Alpes » avant « Rhône »
    return sorted(index, key=lambda x: -len(x[0]))


def resolve_places(question: str) -> list[Place]:
    q = _key(question)
    found: list[Place] = []
    for key, place in _place_index():
        if key not in q:
            continue
        word = key.strip()
        if word in AMBIGUOUS_PLACES and not re.search(rf"\b{re.escape(place.name)}\b", question):
            continue
        found.append(place)
        q = q.replace(key, " # ")   # évite de reconnaître « Rhône » dans « Bouches-du-Rhône »
    return found


# --------------------------------------------------------------------------- choix des tables

def select_tables(question: str, terms: list[Term] | None = None) -> list[str]:
    """Règles simples à partir du glossaire (sera remplacé par une recherche sémantique à l'étape 6)."""
    terms = glossary_for(question) if terms is None else terms
    names = {t.nom for t in terms}
    tables: list[str] = []
    for t in terms:
        for tab in t.tables:
            if tab not in tables:
                tables.append(tab)

    panne = bool(names & {"panne", "anciennete d'une panne", "incident ou maintenance"})
    par_techno = bool(names & {"technologie", "service voix ou data"})

    if "fact_station" in tables and not panne:
        # question de déploiement : pas besoin des indisponibilités
        tables = [t for t in tables if t not in FACTS_INDISPO]
    elif panne and par_techno:
        # pannes par techno ou par service : format long
        tables.append("fact_indispo")
        # garder fact_indispo_site seulement si on a besoin de ses colonnes propres
        if not names & {"anciennete d'une panne", "incident ou maintenance"}:
            tables.remove("fact_indispo_site")
    if not any(t.startswith("fact_") or t.startswith("agg_") for t in tables):
        tables.append(DEFAULT_FACT)

    # faits d'abord, puis dimensions (ordre stable pour le cache du prompt)
    return sorted(dict.fromkeys(tables), key=lambda t: (not t.startswith("fact_"), t))


def pick_examples(tables: list[str], k: int = 2) -> list[Example]:
    """Exemples dont les tables recouvrent le plus celles retenues (simple, avant le RAG)."""
    chosen = set(tables)
    scored = []
    for i, ex in enumerate(load_examples()):
        ex_tables = set(ex.tables)
        if not ex_tables & chosen:
            continue
        score = len(ex_tables & chosen) - 0.5 * len(ex_tables - chosen)
        scored.append((score, -i, ex))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [ex for _, _, ex in scored[:k]]


# --------------------------------------------------------------------------- prompt

@dataclass
class Prompt:
    messages: list[dict]
    tables: list[str]
    terms: list[str]
    examples: list[str]

    @property
    def size_chars(self) -> int:
        return sum(len(m["content"]) for m in self.messages)


def data_context(today: dt.date | None = None) -> str:
    """Dates utiles pour interpréter « hier », « la semaine dernière »..."""
    from netquery.sql_guard import readonly_connection

    today = today or dt.date.today()
    line = f"Date du jour : {today.isoformat()} ({['lundi','mardi','mercredi','jeudi','vendredi','samedi','dimanche'][today.weekday()]})."
    try:
        con = readonly_connection()
        dmin, dmax = con.execute("select min(date), max(date) from fact_indispo_site").fetchone()
        con.close()
        line += f" Relevés d'indisponibilité disponibles du {dmin} au {dmax}."
    except Exception:  # base absente : le prompt reste utilisable
        pass
    return line


def build_prompt(question: str, today: dt.date | None = None) -> Prompt:
    terms = glossary_for(question)
    places = resolve_places(question)
    if places and all(p.kind == "departement" for p in places):
        # les faits portent déjà le code département : inutile d'envoyer dim_departement
        terms = [t for t in terms if t.nom != "departement et region"]
    tables = select_tables(question, terms)
    examples = pick_examples(tables)

    parts = ["Schéma :", render_schema(tables)]
    if terms:
        parts += ["", "Glossaire :", render_glossary(terms)]
    if places:
        parts += ["", "Lieux reconnus dans la question :"] + [f"- {p.hint()}" for p in places]
    if examples:
        parts += ["", "Exemples :"]
        for ex in examples:
            parts += [f"Question : {ex.question}", f"```sql\n{ex.sql}\n```"]
    parts += ["", data_context(today), "", f"Question : {question}"]

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]
    return Prompt(messages, tables, [t.nom for t in terms], [e.question for e in examples])


# --------------------------------------------------------------------------- validation des exemples

def check_examples() -> list[str]:
    from netquery.sql_guard import UnsafeSQLError, execute, readonly_connection, validate

    errors = []
    con = readonly_connection()
    try:
        for ex in load_examples():
            try:
                checked = validate(ex.sql)
                if set(checked.tables) != set(ex.tables):
                    errors.append(f"« {ex.question} » : tables déclarées {ex.tables}, utilisées {checked.tables}")
                res = execute(checked, con=con)
                if res.df.empty:
                    errors.append(f"« {ex.question} » : résultat vide")
            except (UnsafeSQLError, Exception) as e:  # noqa: BLE001 - on veut tout remonter
                errors.append(f"« {ex.question} » : {e}")
    finally:
        con.close()
    return errors


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Prompt envoyé au LLM")
    p.add_argument("question", nargs="?")
    p.add_argument("--check", action="store_true", help="valider et exécuter les exemples")
    args = p.parse_args(argv)
    if args.check:
        errors = check_examples()
        for e in errors:
            print(f"[ERR] {e}")
        print(f"{len(load_examples())} exemples valides" if not errors else f"{len(errors)} erreur(s)")
        return 1 if errors else 0
    if args.question:
        pr = build_prompt(args.question)
        for m in pr.messages:
            print(f"===== {m['role']} =====\n{m['content']}\n")
        print(f"tables : {pr.tables}\n({pr.size_chars} caractères, environ {pr.size_chars // 4} tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
