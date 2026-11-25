"""
Pipeline text-to-SQL : question -> prompt -> LLM -> extraction du SQL -> validation -> exécution.

Usage :
  python -m netquery.pipeline "Combien de sites SFR étaient en panne 4G dans le Rhône la semaine dernière ?"
  python -m netquery.pipeline --show-prompt "..."
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field

import pandas as pd

from netquery.llm import LLM, ChatModel
from netquery.cache import QuestionCache, question_key
from netquery.optimize import latest_date, rewrite_latest_date
from netquery.prompt import Prompt, build_prompt
from netquery.sql_guard import MAX_ROWS, UnsafeSQLError, execute, validate


@dataclass
class Answer:
    question: str
    prompt: Prompt
    raw_output: str = ""
    sql: str | None = None             # SQL validé et exécuté (avec LIMIT)
    df: pd.DataFrame | None = None
    error: str | None = None
    error_kind: str | None = None      # llm, extraction, securite, execution
    truncated: bool = False
    timings: dict[str, float] = field(default_factory=dict)
    tokens: dict[str, int | None] = field(default_factory=dict)
    attempts: list = field(default_factory=list)   # un Attempt par appel au modèle
    cost: dict = field(default_factory=dict)       # octets lus, coût (BigQuery), réécritures appliquées
    from_cache: bool = False                       # SQL repris du cache, sans appel au LLM

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def nb_corrections(self) -> int:
        return max(len(self.attempts) - 1, 0)


_FENCE = re.compile(r"```(?:sql|duckdb)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_START = re.compile(r"\b(with|select)\b", re.IGNORECASE)


def extract_sql(text: str) -> str:
    """Récupère la requête dans la réponse du LLM (bloc ```sql```, sinon à partir du premier SELECT/WITH)."""
    m = _FENCE.search(text)
    if m:
        return m.group(1).strip()
    m = _START.search(text)
    if not m:
        raise ValueError("aucune requête SQL dans la réponse du modèle")
    return text[m.start():].strip()


MAX_RETRIES = 2          # essais de correction après le premier (README : 2 essais max)
MAX_ERROR_CHARS = 600    # message d'erreur renvoyé au modèle (le contexte est limité)

FIX_PROMPT = """La requête précédente a échoué.
Erreur : {error}
Corrige la requête en respectant le schéma et les règles. Réponds uniquement par la requête corrigée dans un bloc ```sql```."""


_MISSING_COL = re.compile(r'Table "(\w+)" does not have a column named "(\w+)"')
_UNKNOWN_COL = re.compile(r'Referenced column "(\w+)" not found')


def explain_error(sql: str | None, error: str) -> str:
    """Ajoute une indication exploitable par un petit modèle aux erreurs de colonne de DuckDB :
    dans quelle table (et sous quel alias) de la requête se trouve la colonne demandée."""
    m = _MISSING_COL.search(error) or _UNKNOWN_COL.search(error)
    if not m or not sql:
        return error
    column = m.groups()[-1]
    try:
        import sqlglot
        from sqlglot import exp

        from netquery.schema import load

        tree = sqlglot.parse_one(sql, read="duckdb")
        aliases = {t.alias_or_name: t.name for t in tree.find_all(exp.Table)}
    except Exception:  # noqa: BLE001 - l'indication est un bonus
        return error
    tables = load().tables
    owners = [f"{name} (alias {alias})" if alias != name else name
              for alias, name in aliases.items()
              if name in tables and any(c.name == column for c in tables[name].columns)]
    if owners:
        hint = f"La colonne {column} se trouve dans : {', '.join(owners)}. Préfixe-la avec le bon alias."
    else:
        others = [n for n, t in tables.items() if any(c.name == column for c in t.columns)]
        hint = (f"Aucune table de la requête n'a de colonne {column}"
                + (f" ; elle existe dans : {', '.join(others)}." if others else " ; elle n'existe pas dans le schéma."))
    return f"{error.splitlines()[0]}\n{hint}"


@dataclass
class Attempt:
    sql: str | None
    error: str | None
    error_kind: str | None


BACKEND = os.getenv("NETQUERY_BACKEND", "duckdb").lower()   # duckdb (local) ou bigquery


def _try_once(text: str, backend: str = BACKEND) -> dict:
    """Extraction, garde-fous, réécriture de coût, exécution sur le moteur choisi."""
    out = {"sql": None, "df": None, "truncated": False, "exec_s": 0.0, "error": None, "kind": None, "cost": {}}
    try:
        sql = extract_sql(text)
    except ValueError as e:
        return out | {"error": str(e), "kind": "extraction"}
    try:
        checked = validate(sql)
    except UnsafeSQLError as e:
        return out | {"sql": sql, "error": f"requête refusée : {e}", "kind": "securite"}
    checked, n_rewrites = rewrite_latest_date(checked)
    out["sql"] = checked.sql
    out["cost"]["reecritures"] = n_rewrites
    try:
        if backend == "bigquery":
            from netquery import bigquery_backend as bq

            res = bq.execute(checked)
            out["cost"].update(bytes_processed=res.estimate.bytes_processed, bytes_billed=res.estimate.bytes_billed,
                               cost_usd=res.estimate.cost_usd, cache_hit=res.cache_hit, sql_bigquery=res.estimate.sql)
            truncated = len(res.df) >= MAX_ROWS
        else:
            res = execute(checked)
            truncated = res.truncated
    except Exception as e:  # noqa: BLE001 - erreur SQL, délai dépassé, coût trop élevé
        from netquery.bigquery_backend import CostLimitError
        kind = "cout" if isinstance(e, CostLimitError) else "execution"
        return out | {"error": f"erreur d'exécution : {e}" if kind == "execution" else f"requête refusée : {e}",
                      "kind": kind}
    return out | {"df": res.df, "truncated": truncated, "exec_s": res.elapsed_s}


def ask(question: str, llm: ChatModel | None = None, max_retries: int = MAX_RETRIES,
        use_rag: bool = True, naive: bool = False, backend: str | None = None,
        cache: QuestionCache | None = None) -> Answer:
    """cache : si fourni, une question déjà posée réutilise son SQL validé sans appeler le LLM."""
    t0 = time.perf_counter()
    backend = backend or BACKEND
    prompt = build_prompt(question, use_rag=use_rag, naive=naive)
    ans = Answer(question=question, prompt=prompt)
    ans.timings["prompt_s"] = time.perf_counter() - t0
    ans.timings["llm_s"] = 0.0
    messages = list(prompt.messages)

    key = None
    if cache is not None:
        llm = llm or LLM()
        key = question_key(question, getattr(llm, "model", "inconnu"), latest_date().isoformat())
        cached = cache.get(key)
        if cached:
            r = _try_once(f"```sql\n{cached}\n```", backend=backend)
            if r["error"] is None:
                ans.sql, ans.df, ans.truncated, ans.cost = r["sql"], r["df"], r["truncated"], r["cost"]
                ans.from_cache = True
                ans.timings["exec_s"] = r["exec_s"]
                ans.timings["total_s"] = time.perf_counter() - t0
                return ans
            cache.drop(key)  # SQL devenu invalide (modèle de données modifié) : on régénère

    for attempt in range(max_retries + 1):
        # 1. génération (ou correction)
        try:
            llm = llm or LLM()
            completion = llm.complete(messages)
        except Exception as e:  # noqa: BLE001 - serveur absent, délai dépassé...
            ans.error, ans.error_kind = f"appel au modèle impossible : {e}", "llm"
            return ans
        ans.raw_output = completion.text
        ans.timings["llm_s"] += completion.latency_s
        if attempt == 0:
            ans.tokens = {"prompt": completion.prompt_tokens, "completion": completion.completion_tokens}

        # 2. extraction, garde-fous, exécution
        r = _try_once(completion.text, backend=backend)
        sql, error, kind = r["sql"], r["error"], r["kind"]
        ans.attempts.append(Attempt(sql, error, kind))
        ans.sql, ans.error, ans.error_kind, ans.cost = sql, error, kind, r["cost"]
        if error is None:
            ans.df, ans.truncated = r["df"], r["truncated"]
            ans.timings["exec_s"] = r["exec_s"]
            if cache is not None and key:
                cache.put(key, question, sql)
            break
        if kind == "cout":
            break  # une requête trop coûteuse n'est pas une erreur que le modèle sait corriger

        # 3. on renvoie l'erreur au modèle pour qu'il corrige
        messages += [
            {"role": "assistant", "content": completion.text},
            {"role": "user", "content": FIX_PROMPT.format(error=explain_error(sql, error)[:MAX_ERROR_CHARS])},
        ]
    ans.timings["total_s"] = time.perf_counter() - t0
    return ans


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Poser une question en français")
    p.add_argument("question")
    p.add_argument("--show-prompt", action="store_true")
    args = p.parse_args(argv)

    ans = ask(args.question)
    if args.show_prompt:
        for m in ans.prompt.messages:
            print(f"===== {m['role']} =====\n{m['content']}\n")
    print(f"tables : {', '.join(ans.prompt.tables)}")
    for i, a in enumerate(ans.attempts[:-1], 1):
        print(f"essai {i} corrigé : [{a.error_kind}] {a.error}")
    if ans.sql:
        print(f"\n{ans.sql}\n")
    if not ans.ok:
        print(f"[{ans.error_kind}] {ans.error}")
        if ans.raw_output and not ans.sql:
            print(f"réponse du modèle :\n{ans.raw_output}")
        return 1
    with pd.option_context("display.max_rows", 30, "display.width", 160):
        print(ans.df)
    t = ans.timings
    print(f"\nLLM {t.get('llm_s', 0):.1f} s, exécution {t.get('exec_s', 0) * 1000:.0f} ms, "
          f"tokens prompt {ans.tokens.get('prompt')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
