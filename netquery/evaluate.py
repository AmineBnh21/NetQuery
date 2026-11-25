"""
Evaluation sur eval/questions.yaml : exactitude d'exécution, latence, tokens, corrections.

Une réponse est juste si son résultat contient celui de la requête de référence :
  - même nombre de lignes ;
  - chaque colonne de référence (ou chaque colonne "cles") a son équivalent dans la réponse,
    comparé comme un multiensemble de valeurs (l'ordre des lignes et des colonnes, les noms de colonnes
    et les colonnes en plus ne comptent pas) ;
  - nombres arrondis à 0,1 près ; dates comparées au jour.

Configurations :
  naive     schéma complet, ni glossaire, ni lieux, ni exemples, pas de correction
  sans_rag  version optimisée, exemples choisis par recouvrement des tables
  optimise  version complète (règles + RAG + lieux + 2 corrections)

Usage :
  python -m netquery.evaluate --check                    # valider les requêtes de référence
  python -m netquery.evaluate --config naive optimise    # lancer (reprend là où un passage s'est arrêté)
  python -m netquery.evaluate --report                   # régénérer eval/rapport.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd
import yaml

from netquery.sql_guard import execute, readonly_connection, validate

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = ROOT / "eval"
QUESTIONS_PATH = EVAL_DIR / "questions.yaml"
RESULTS_DIR = EVAL_DIR / "results"
REPORT_PATH = EVAL_DIR / "rapport.md"

CONFIGS = {
    "naive": {"naive": True, "max_retries": 0},
    "sans_rag": {"use_rag": False, "max_retries": 2},
    "optimise": {"use_rag": True, "max_retries": 2},
}


@dataclass
class Question:
    id: str
    categorie: str
    question: str
    sql: str
    cles: list[str] | None = None
    alternatives: list[str] | None = None


def load_questions(path: Path = QUESTIONS_PATH) -> list[Question]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Question(q["id"], q["categorie"], q["question"], q["sql"].strip(), q.get("cles"),
                     [a.strip() for a in q.get("alternatives") or []])
            for q in data["questions"]]


# --------------------------------------------------------------------------- comparaison des résultats

def _norm(v):
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT:
        return None
    if isinstance(v, Decimal):
        v = float(v)
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):  # types numpy
        v = v.item()
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        r = round(float(v), 1)
        return int(r) if r.is_integer() else r
    if isinstance(v, (pd.Timestamp, dt.datetime)):
        if v.tzinfo is not None:  # BigQuery renvoie des horodatages UTC explicites, DuckDB naïfs
            v = v.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return v.date().isoformat() if v.time() == dt.time(0) else v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return str(v).strip()


def _column_bag(df: pd.DataFrame, col) -> Counter:
    return Counter(map(_norm, df[col].tolist()))


def same_result(gold: pd.DataFrame, pred: pd.DataFrame, keys: list[str] | None = None) -> bool:
    if pred is None or len(gold) != len(pred):
        return False
    gold_cols = keys or list(gold.columns)
    pred_bags = [_column_bag(pred, c) for c in pred.columns]
    used: set[int] = set()
    for col in gold_cols:
        bag = _column_bag(gold, col)
        match = next((i for i, pb in enumerate(pred_bags) if i not in used and pb == bag), None)
        if match is None:
            return False
        used.add(match)
    return True


def gold_result(q: Question, con) -> pd.DataFrame:
    return execute(validate(q.sql), con=con).df


def gold_results(q: Question, con) -> list[pd.DataFrame]:
    """Référence principale puis alternatives également justes."""
    return [gold_result(q, con)] + [execute(validate(a), con=con).df for a in q.alternatives or []]


def is_correct(q: Question, pred: pd.DataFrame | None, con) -> bool:
    return pred is not None and any(same_result(g, pred, q.cles) for g in gold_results(q, con))


# --------------------------------------------------------------------------- exécution d'une configuration

@dataclass
class Record:
    id: str
    categorie: str
    config: str
    correct: bool
    error_kind: str | None
    error: str | None
    sql: str | None
    llm_s: float
    exec_s: float
    total_s: float
    prompt_tokens: int | None
    prompt_chars: int
    corrections: int
    tables: list[str]


def results_path(config: str) -> Path:
    return RESULTS_DIR / f"{config}.jsonl"


def load_records(config: str) -> list[dict]:
    p = results_path(config)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_config(config: str, questions: list[Question], restart: bool = False) -> None:
    from netquery.llm import LLM
    from netquery.pipeline import ask

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = results_path(config)
    if restart and path.exists():
        ids = {q.id for q in questions}
        kept = [r for r in load_records(config) if r["id"] not in ids]   # ne repart de zéro que pour ces questions
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept), encoding="utf-8")
    done = {r["id"] for r in load_records(config)}
    llm = LLM()
    con = readonly_connection()
    try:
        todo = [q for q in questions if q.id not in done]
        for n, q in enumerate(todo, 1):
            t0 = time.perf_counter()
            ans = ask(q.question, llm=llm, **CONFIGS[config])
            ok = ans.ok and is_correct(q, ans.df, con)
            rec = Record(
                id=q.id, categorie=q.categorie, config=config, correct=ok,
                error_kind=ans.error_kind if not ans.ok else (None if ok else "resultat"),
                error=ans.error, sql=ans.sql,
                llm_s=round(ans.timings.get("llm_s", 0.0), 2),
                exec_s=round(ans.timings.get("exec_s", 0.0), 4),
                total_s=round(time.perf_counter() - t0, 2),
                prompt_tokens=ans.tokens.get("prompt"), prompt_chars=ans.prompt.size_chars,
                corrections=ans.nb_corrections, tables=ans.prompt.tables,
            )
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
            print(f"[{config}] {n}/{len(todo)} {q.id} {'OK ' if ok else 'KO '} "
                  f"{rec.total_s:5.1f} s  {rec.error_kind or ''}", flush=True)
    finally:
        con.close()


# --------------------------------------------------------------------------- rapport

# --------------------------------------------------------------------------- coût sur BigQuery

def bq_path(config: str) -> Path:
    return RESULTS_DIR / f"{config}_bigquery.jsonl"


def measure_bigquery(config: str, rewrite: bool = False) -> None:
    """Dry run BigQuery de chaque requête générée (gratuit, rien n'est exécuté) : octets lus et coût.
    config 'reference' mesure les requêtes de référence écrites à la main.
    rewrite=True applique d'abord la réécriture « dernier relevé » (résultats enregistrés sous <config>_reecrit)."""
    from netquery import bigquery_backend as bq
    from netquery.optimize import rewrite_latest_date

    if config == "reference":
        items = [(q.id, q.sql, True) for q in load_questions()]
    else:
        items = [(r["id"], r["sql"], r["correct"]) for r in load_records(config) if r["sql"]]
    out = []
    for id_, sql, correct in items:
        rec = {"id": id_, "correct": correct}
        try:
            checked = validate(sql)
            if rewrite:
                checked, _ = rewrite_latest_date(checked)
            est = bq.estimate(checked)
            rec.update(bytes_processed=est.bytes_processed, bytes_billed=est.bytes_billed,
                       cost_usd=est.cost_usd, tables=est.tables)
        except Exception as e:  # noqa: BLE001 - SQL invalide : pas de coût mesurable
            rec["error"] = str(e).splitlines()[0][:200]
        out.append(rec)
    name = f"{config}_reecrit" if rewrite else config
    bq_path(name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")
    ok = [r for r in out if "bytes_processed" in r]
    print(f"[{name}] {len(ok)}/{len(out)} requêtes estimées, "
          f"{sum(r['bytes_processed'] for r in ok) / 1e6:.0f} Mo lus au total")


def _bq_summary(config: str) -> dict | None:
    p = bq_path(config)
    if not p.exists():
        return None
    rs = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    ok = [r for r in rs if "bytes_processed" in r]
    if not ok:
        return None
    return {
        "n": len(ok),
        "mo_moy": statistics.mean(r["bytes_processed"] for r in ok) / 1e6,
        "mo_med": statistics.median(r["bytes_processed"] for r in ok) / 1e6,
        "mo_facture_moy": statistics.mean(r["bytes_billed"] for r in ok) / 1e6,
        "cout_1000": 1000 * statistics.mean(r["cost_usd"] for r in ok),
    }


def _summary(records: list[dict]) -> dict:
    n = len(records)
    lat = [r["total_s"] for r in records]
    toks = [r["prompt_tokens"] for r in records if r["prompt_tokens"]]
    return {
        "n": n,
        "exactitude": sum(r["correct"] for r in records) / n if n else 0.0,
        "latence_moy": statistics.mean(lat) if lat else 0.0,
        "latence_med": statistics.median(lat) if lat else 0.0,
        "tokens_moy": statistics.mean(toks) if toks else 0.0,
        "corrections": sum(r["corrections"] > 0 for r in records),
        "erreurs": Counter(r["error_kind"] for r in records if not r["correct"]),
    }


def write_report(configs: list[str] | None = None) -> str:
    questions = {q.id: q for q in load_questions()}
    configs = [c for c in (configs or list(CONFIGS)) if load_records(c)]
    by_conf = {c: load_records(c) for c in configs}
    lines = ["# Rapport d'évaluation", "",
             f"Généré le {dt.datetime.now():%Y-%m-%d %H:%M}, {len(questions)} questions "
             f"(`eval/questions.yaml`), modèle local via Ollama.", "",
             "| Version | Questions | Exactitude | Latence moyenne | Latence médiane | Tokens prompt moyens | Questions corrigées |",
             "|---|---|---|---|---|---|---|"]
    for c in configs:
        s = _summary(by_conf[c])
        lines.append(f"| {c} | {s['n']} | **{100 * s['exactitude']:.0f} %** | {s['latence_moy']:.1f} s | "
                     f"{s['latence_med']:.1f} s | {s['tokens_moy']:.0f} | {s['corrections']} |")

    bq_rows = [(c, _bq_summary(c)) for c in ["reference", "reference_reecrit", *configs,
                                              *[f"{c}_reecrit" for c in configs]]]
    if any(s for _, s in bq_rows):
        lines += ["", "## Coût sur BigQuery (dry run)", "",
                  "Octets lus estimés par BigQuery pour chaque requête générée ; facturé = avec le minimum de "
                  "10 Mo par table lue ; coût au tarif à la demande (6,25 USD par Tio).", "",
                  "| Version | Requêtes estimées | Mo lus (moyenne) | Mo lus (médiane) | Mo facturés (moyenne) | Coût pour 1000 questions |",
                  "|---|---|---|---|---|---|"]
        for c, s in bq_rows:
            if s:
                lines.append(f"| {c} | {s['n']} | {s['mo_moy']:.1f} | {s['mo_med']:.1f} | "
                             f"{s['mo_facture_moy']:.1f} | {s['cout_1000']:.3f} USD |")

    cats = sorted({q.categorie for q in questions.values()})
    lines += ["", "## Exactitude par catégorie", "",
              "| Catégorie | " + " | ".join(configs) + " |", "|---|" + "---|" * len(configs)]
    for cat in cats:
        cells = []
        for c in configs:
            rs = [r for r in by_conf[c] if r["categorie"] == cat]
            cells.append(f"{sum(r['correct'] for r in rs)}/{len(rs)}" if rs else "")
        lines.append(f"| {cat} | " + " | ".join(cells) + " |")

    lines += ["", "## Types d'erreur", "",
              "`resultat` : la requête s'exécute mais le résultat diffère de la référence ; "
              "`execution` : erreur SQL ; `securite` : refusée par les garde-fous ; "
              "`extraction` : pas de SQL dans la réponse.", "",
              "| Version | " + " | ".join(["resultat", "execution", "securite", "extraction", "llm"]) + " |",
              "|---|---|---|---|---|---|"]
    for c in configs:
        e = _summary(by_conf[c])["erreurs"]
        lines.append(f"| {c} | " + " | ".join(str(e.get(k, 0)) for k in
                                               ["resultat", "execution", "securite", "extraction", "llm"]) + " |")

    if configs:
        last = configs[-1]
        fails = [r for r in by_conf[last] if not r["correct"]]
        lines += ["", f"## Echecs de la version `{last}`", ""]
        for r in fails:
            q = questions.get(r["id"])
            lines.append(f"- **{r['id']}** ({r['error_kind']}) {q.question if q else ''}")
    text = "\n".join(lines) + "\n"
    REPORT_PATH.write_text(text, encoding="utf-8")
    return text


# --------------------------------------------------------------------------- validation du jeu

def check_questions() -> list[str]:
    from netquery.prompt import load_examples

    errors = []
    questions = load_questions()
    ids = Counter(q.id for q in questions)
    errors += [f"identifiant en double : {i}" for i, n in ids.items() if n > 1]
    examples = {e.question.strip().lower() for e in load_examples()}
    con = readonly_connection()
    try:
        for q in questions:
            if q.question.strip().lower() in examples:
                errors.append(f"{q.id} : question identique à un exemple du prompt")
            try:
                df = gold_result(q, con)
                alts = gold_results(q, con)[1:]
            except Exception as e:  # noqa: BLE001
                errors.append(f"{q.id} : {e}")
                continue
            if any(a.empty for a in alts):
                errors.append(f"{q.id} : une alternative a un résultat vide")
            if df.empty:
                errors.append(f"{q.id} : résultat de référence vide")
            if df.shape == (1, 1) and df.iloc[0, 0] in (0, None):
                errors.append(f"{q.id} : réponse de référence nulle (question peu discriminante)")
            if q.cles and not set(q.cles) <= set(df.columns):
                errors.append(f"{q.id} : clés absentes du résultat {q.cles}")
            if not same_result(df, df, q.cles):
                errors.append(f"{q.id} : la référence ne se reconnaît pas elle-même")
    finally:
        con.close()
    return errors


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Evaluation de NetQuery")
    p.add_argument("--check", action="store_true", help="valider les requêtes de référence")
    p.add_argument("--config", nargs="+", choices=list(CONFIGS), help="configurations à évaluer")
    p.add_argument("--only", nargs="+", help="identifiants de questions")
    p.add_argument("--restart", action="store_true", help="repartir de zéro pour ces configurations")
    p.add_argument("--report", action="store_true", help="régénérer le rapport")
    args = p.parse_args(argv)

    if args.check:
        errors = check_questions()
        for e in errors:
            print(f"[ERR] {e}")
        print(f"{len(load_questions())} questions valides" if not errors else f"{len(errors)} erreur(s)")
        return 1 if errors else 0
    if args.config:
        questions = load_questions()
        if args.only:
            questions = [q for q in questions if q.id in set(args.only)]
        for c in args.config:
            run_config(c, questions, restart=args.restart)
        args.report = True
    if args.report:
        print(write_report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
