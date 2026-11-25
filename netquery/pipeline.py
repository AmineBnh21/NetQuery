"""
Pipeline text-to-SQL : question -> prompt -> LLM -> extraction du SQL -> validation -> exécution.

Usage :
  python -m netquery.pipeline "Combien de sites SFR étaient en panne 4G dans le Rhône la semaine dernière ?"
  python -m netquery.pipeline --show-prompt "..."
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field

import pandas as pd

from netquery.llm import LLM, ChatModel
from netquery.prompt import Prompt, build_prompt
from netquery.sql_guard import UnsafeSQLError, execute, validate


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

    @property
    def ok(self) -> bool:
        return self.error is None


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


def ask(question: str, llm: ChatModel | None = None) -> Answer:
    t0 = time.perf_counter()
    prompt = build_prompt(question)
    ans = Answer(question=question, prompt=prompt)
    ans.timings["prompt_s"] = time.perf_counter() - t0

    # 1. génération
    try:
        llm = llm or LLM()
        completion = llm.complete(prompt.messages)
    except Exception as e:  # noqa: BLE001 - serveur absent, délai dépassé...
        ans.error, ans.error_kind = f"appel au modèle impossible : {e}", "llm"
        return ans
    ans.raw_output = completion.text
    ans.timings["llm_s"] = completion.latency_s
    ans.tokens = {"prompt": completion.prompt_tokens, "completion": completion.completion_tokens}

    # 2. extraction puis garde-fous
    try:
        sql = extract_sql(completion.text)
    except ValueError as e:
        ans.error, ans.error_kind = str(e), "extraction"
        return ans
    try:
        checked = validate(sql)
    except UnsafeSQLError as e:
        ans.sql, ans.error, ans.error_kind = sql, f"requête refusée : {e}", "securite"
        return ans
    ans.sql = checked.sql

    # 3. exécution en lecture seule
    try:
        res = execute(checked)
    except Exception as e:  # noqa: BLE001 - erreur SQL, délai dépassé
        ans.error, ans.error_kind = f"erreur d'exécution : {e}", "execution"
        return ans
    ans.df, ans.truncated = res.df, res.truncated
    ans.timings["exec_s"] = res.elapsed_s
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
