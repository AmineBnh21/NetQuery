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

from netquery.schema import (SEMANTIC_DIR, Term, glossary_for, load, normalize, render_glossary,
                             render_schema)

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


OPERATOR_WORDS = {"orange": "ORANGE", "sfr": "SFR", "bouygues": "BOUYGUES", "bytel": "BOUYGUES", "free": "FREE"}


def operator_hints(question: str, places: list[Place]) -> list[str]:
    """En outre-mer les opérateurs ont d'autres codes (Free aux Antilles = FREE_CARAIBES) :
    on lit dans dim_site les opérateurs présents dans les départements cités."""
    deps = [p.code for p in places if p.kind == "departement" and p.code[:2] in ("97", "98")]
    if not deps:
        return []
    from netquery.sql_guard import readonly_connection

    try:
        con = readonly_connection()
        present = [r[0] for r in con.execute(
            "select distinct operateur_id from dim_site where departement in ? order by 1", [deps]).fetchall()]
        con.close()
    except Exception:  # noqa: BLE001
        return []
    hints = []
    words = set(_key(question).split())
    for word, code in OPERATOR_WORDS.items():
        if word in words:
            local = [op for op in present if op.startswith(code)]
            if local and local != [code]:
                hints.append(f"{word.capitalize()} dans ce département = operateur_id {' ou '.join(repr(o) for o in local)}")
    hints.append(f"Opérateurs présents : {', '.join(present)}")
    return hints


# --------------------------------------------------------------------------- choix des tables

def select_tables(question: str, terms: list[Term] | None = None,
                  fallback: list[str] | None = None) -> list[str]:
    """Règles à partir du glossaire. Si aucune règle ne désigne de table principale (fait ou dim_site),
    on prend `fallback` (tables de l'exemple le plus proche, voir rag_tables), sinon fact_indispo_site."""
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
    if not any(t.startswith(("fact_", "agg_")) or t == "dim_site" for t in tables):
        if "dates relatives" not in names and "calendrier" not in names:
            # ni panne, ni déploiement, ni date : la question porte sur le parc de sites
            tables.append("dim_site")
        else:
            tables += fallback or [DEFAULT_FACT]

    # faits d'abord, puis dimensions (ordre stable pour le cache du prompt)
    return sorted(dict.fromkeys(tables), key=lambda t: (not t.startswith("fact_"), t))


def _main_tables(tables) -> list[str]:
    return [t for t in tables if t.startswith(("fact_", "agg_")) or t == "dim_site"]


def similar_examples(question: str, k: int = 6, exclude: set[int] | None = None) -> list[tuple[int, Example]]:
    """Exemples les plus proches de la question (recherche hybride BM25 + embeddings)."""
    from netquery.retrieval import example_index

    examples = load_examples()
    return [(h.index, examples[h.index]) for h in example_index().search(question, k=k, exclude=exclude)]


def rag_select(question: str, terms: list[Term], k: int = 2,
               exclude: set[int] | None = None) -> tuple[list[str], list[Example]]:
    """Tables et exemples few-shot.
    Tables : règles du glossaire, avec repli sur la table principale des exemples proches.
    Exemples : d'abord la structure (mêmes tables exactement, puis un sous-ensemble), ensuite la
    similarité de la question. Mesuré : la similarité seule choisit des exemples proches en mots
    mais de structure différente, que le petit modèle recopie."""
    n = len(load_examples())
    neighbours = [ex for _, ex in similar_examples(question, k=n, exclude=exclude)]  # tous, classés
    fallback = next((_main_tables(ex.tables) for ex in neighbours if _main_tables(ex.tables)), None)
    tables = select_tables(question, terms, fallback=fallback)
    chosen = set(tables)

    def structure(ex: Example) -> int:
        ts = set(ex.tables)
        return 0 if ts == chosen else 1 if ts <= chosen else 2 if ts & set(_main_tables(chosen)) else 3

    ranked = sorted(neighbours, key=structure)  # tri stable : l'ordre de similarité départage
    return tables, [ex for ex in ranked if structure(ex) < 3][:k]


def pick_examples(tables: list[str], k: int = 2) -> list[Example]:
    """Ancienne sélection par recouvrement des tables (sans RAG), gardée pour comparer à l'étape 8."""
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


# --------------------------------------------------------------------------- questions de suivi

_FOLLOWUP_START = re.compile(
    r"^\s*(et|meme chose|pareil|idem|plutot|maintenant|seulement|uniquement|par|pour|en|dans|sur)\b")
_FOLLOWUP_ANY = re.compile(r"\b(seulement|uniquement|a la place|au lieu de|meme question|pareil)\b")


def is_followup(question: str) -> bool:
    """Question qui n'a de sens qu'avec la précédente : « Et pour Bouygues ? », « Par région ? »,
    « Seulement la 5G ». Règles simples (pas d'appel au LLM) : début de phrase typique, ou mot de
    restriction dans une question courte."""
    q = normalize(question)
    n_words = len(re.findall(r"[a-z0-9]+", q))
    if q.lstrip().startswith("et ") or q.strip() in ("et", "et ?"):
        return True
    return n_words <= 7 and bool(_FOLLOWUP_START.search(q) or _FOLLOWUP_ANY.search(q))


@dataclass
class Turn:
    """Echange précédent réussi, utilisé comme contexte d'une question de suivi."""
    question: str
    sql: str


def build_prompt(question: str, today: dt.date | None = None, use_rag: bool = True,
                 naive: bool = False, previous: Turn | None = None) -> Prompt:
    """naive=True : version de référence pour l'évaluation (schéma complet, ni glossaire,
    ni lieux, ni exemples).
    previous : échange précédent, si `question` est une question de suivi."""
    if naive:
        tables = list(load().tables)
        parts = ["Schéma :", render_schema(tables), "", data_context(today), "", f"Question : {question}"]
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(parts)}]
        return Prompt(messages, tables, [], [])

    # pour une question de suivi, le contexte (tables, glossaire, exemples) vient des deux questions ;
    # un lieu cité dans la suite remplace celui de la question précédente
    selection = f"{previous.question} {question}" if previous else question
    terms = glossary_for(selection)
    places = resolve_places(question) or (resolve_places(previous.question) if previous else [])
    if places and all(p.kind == "departement" for p in places):
        # les faits portent déjà le code département : inutile d'envoyer dim_departement
        terms = [t for t in terms if t.nom != "departement et region"]
    if use_rag:
        tables, examples = rag_select(selection, terms)
    else:
        tables = select_tables(selection, terms)
        examples = pick_examples(tables)
    if previous:
        # les tables de la requête précédente restent disponibles pour la modifier
        from netquery.sql_guard import validate

        try:
            tables = sorted(set(tables) | set(validate(previous.sql).tables),
                            key=lambda t: (not t.startswith("fact_"), t))
        except Exception:  # noqa: BLE001
            pass
        examples = examples[:1]  # le SQL précédent sert d'exemple principal

    parts = ["Schéma :", render_schema(tables)]
    if terms:
        parts += ["", "Glossaire :", render_glossary(terms)]
    if places:
        parts += ["", "Lieux reconnus dans la question :"] + [f"- {p.hint()}" for p in places]
        parts += [f"- {h}" for h in operator_hints(selection, places)]
    if examples:
        parts += ["", "Exemples :"]
        for ex in examples:
            parts += [f"Question : {ex.question}", f"```sql\n{ex.sql}\n```"]
    parts += ["", data_context(today), ""]
    if previous:
        parts += [f"Question précédente : {previous.question}", "Requête précédente :",
                  f"```sql\n{previous.sql}\n```",
                  f"Question de suivi : {question}",
                  "Réécris la requête précédente en appliquant uniquement la modification demandée "
                  "par la question de suivi ; garde tous les autres filtres."]
    else:
        parts += [f"Question : {question}"]

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
