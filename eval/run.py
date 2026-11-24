"""Run the labelled question set through the agent and score execution accuracy.

    python -m eval.run                         # all configs (main, effort sweep, ablation)
    python -m eval.run --configs main          # just the main config
    python -m eval.run --models claude-opus-5-5,claude-sonnet-5-5   # per-model comparison
    python -m eval.run --client mock --seed    # offline pipeline check (CI)

Configs:
  main          effort from AGENT_EFFORT (default medium), schema retrieval on
  effort_low    effort=low,  retrieval on
  effort_high   effort=high, retrieval on
  no_retrieval  effort as main, every schema pasted into the system prompt and the
                search_schema/describe_table tools removed (retrieval ablation)

Outputs: eval/results/latest.json, latest.md and failures.md for a real run;
eval/results/mock_<db>.json/.md for the mock client. Every run is also archived with
full traces in eval/results/runs/<timestamp>/ (git-ignored).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from agent.config import ROOT, Settings, load_settings, seed_settings
from agent.db import ReadOnlyDB, to_jsonable
from agent.loop import Agent
from agent.schema_index import SchemaIndex
from agent.sql_guard import GuardRejection, SqlGuard, SqlSyntaxError
from agent.tools import Toolbox
from eval.attacks import guard_scores
from eval.compare import compare, is_abstention
from eval.mock_client import GoldReplayClient
from eval.report import write_reports
from eval.retrieval_probes import probe_scores

QUESTIONS = ROOT / "eval" / "questions.jsonl"
RESULTS = ROOT / "eval" / "results"
EVAL_ROW_LIMIT = 100_000

CONFIGS = {
    "main": {},
    "effort_low": {"effort": "low"},
    "effort_high": {"effort": "high"},
    "no_retrieval": {"schema_retrieval": False},
}


def load_questions(path: Path = QUESTIONS) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class Executor:
    """Runs gold and predicted SQL on the same read-only DB, through the same guard."""

    def __init__(self, db: ReadOnlyDB, index: SchemaIndex):
        self.db = db
        self.guard = SqlGuard(index.allowed_tables, db.catalog, db.statement_types,
                              row_limit=EVAL_ROW_LIMIT, known_tables=db.relation_names())

    def run(self, sql: str):
        checked = self.guard.check(sql)
        res = self.db.execute(checked.sql, max_rows=EVAL_ROW_LIMIT, timeout_s=60)
        return res.columns, [tuple(r) for r in res.rows]


def score(q: dict, r, ex: Executor, gold_cache: dict) -> dict:
    rec = {
        "id": q["id"], "difficulty": q["difficulty"],
        "bucket": "unanswerable" if q["unanswerable"] else q["difficulty"],
        "question": q["question"], "gold_sql": q["gold_sql"], "pred_sql": r.sql,
        "answer": r.answer, "assumptions": r.assumptions, "status": r.status,
        "error": r.error, "error_kind": r.error_kind, "turns": r.turns,
        "tool_calls": r.tool_calls, "sql_attempts": len(r.sql_attempts),
        "sql_errors": r.sql_errors, "first_sql_ok": r.first_sql_ok, "retries": r.retries,
        "recovered": r.recovered, "guard_rejections": r.guard_rejections,
        "fallback_used": r.fallback_used, "usage": r.usage, "cost_usd": r.cost_usd,
        "latency_ms": r.latency_ms,
    }
    if q["unanswerable"]:
        rec["correct"] = is_abstention(r.sql, r.answer) and r.status == "abstained"
        rec["match_reason"] = "abstained" if rec["correct"] else (
            "answered an unanswerable question" if r.sql else "empty SQL but the answer did "
            "not say it cannot be answered")
        return rec
    if r.status != "answered":
        rec["correct"] = False
        rec["match_reason"] = ("abstained on an answerable question" if r.status == "abstained"
                               else f"no answer ({r.error_kind})")
        return rec
    if q["id"] not in gold_cache:
        gold_cache[q["id"]] = ex.run(q["gold_sql"])[1]
    try:
        _, pred_rows = ex.run(r.sql)
    except (GuardRejection, SqlSyntaxError, Exception) as e:  # noqa: BLE001
        rec["correct"] = False
        rec["match_reason"] = f"predicted SQL failed on re-run: {e}"
        return rec
    m = compare(gold_cache[q["id"]], pred_rows, q.get("order_matters", False))
    rec.update(correct=m.ok, match_reason=m.reason, extra_columns=m.extra_columns,
               pred_row_count=len(pred_rows), gold_row_count=len(gold_cache[q["id"]]))
    return rec


def pct(n: int | float, d: int | float) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def p(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def summarize(recs: list[dict]) -> dict:
    ans = [r for r in recs if r["bucket"] != "unanswerable"]
    una = [r for r in recs if r["bucket"] == "unanswerable"]
    with_sql = [r for r in recs if r["sql_attempts"] > 0]
    errored = [r for r in with_sql if r["sql_errors"] > 0]
    recovered = [r for r in errored if r["recovered"]]
    by_diff = {}
    for b in ("easy", "medium", "hard"):
        rs = [r for r in ans if r["bucket"] == b]
        by_diff[b] = {"n": len(rs), "correct": sum(r["correct"] for r in rs),
                      "accuracy_pct": pct(sum(r["correct"] for r in rs), len(rs))}
    lat = [r["latency_ms"] / 1000 for r in recs]
    tok = lambda k: [r["usage"].get(k, 0) for r in recs]  # noqa: E731
    total_in = [a + b + c for a, b, c in zip(tok("input_tokens"), tok("cache_read_input_tokens"),
                                              tok("cache_creation_input_tokens"), strict=True)]
    costs = [r["cost_usd"] for r in recs if r["cost_usd"] is not None]
    kinds: dict[str, int] = {}
    for r in recs:
        if r["status"] == "error":
            k = (r["error_kind"] or "unknown").split(":")[0]
            kinds[k] = kinds.get(k, 0) + 1
    return {
        "n": len(recs),
        "n_answerable": len(ans),
        "n_unanswerable": len(una),
        "execution_accuracy_pct": pct(sum(r["correct"] for r in recs), len(recs)),
        "answerable_accuracy_pct": pct(sum(r["correct"] for r in ans), len(ans)),
        "by_difficulty": by_diff,
        "abstention_accuracy_pct": pct(sum(r["correct"] for r in una), len(una)),
        "false_abstentions": sum(r["status"] == "abstained" for r in ans),
        "valid_sql_first_attempt_pct": pct(sum(bool(r["first_sql_ok"]) for r in with_sql),
                                           len(with_sql)),
        "valid_sql_final_pct": pct(sum(r["status"] == "answered" for r in with_sql),
                                   len(with_sql)),
        "self_correction": {
            "questions_with_sql": len(with_sql),
            "needed_retry_pct": pct(len(errored), len(with_sql)),
            "mean_retries": round(statistics.mean(r["retries"] for r in with_sql), 2)
            if with_sql else None,
            "recovered_pct": pct(len(recovered), len(errored)),
            "recovered_and_correct_pct": pct(sum(r["correct"] for r in recovered), len(errored)),
        },
        "guard_rejections": sum(r["guard_rejections"] for r in recs),
        "errors_by_kind": kinds,
        "mean_turns": round(statistics.mean(r["turns"] for r in recs), 2) if recs else None,
        "mean_tool_calls": round(statistics.mean(r["tool_calls"] for r in recs), 2)
        if recs else None,
        "tokens": {
            "mean_input_total": round(statistics.mean(total_in)) if recs else None,
            "mean_uncached_input": round(statistics.mean(tok("input_tokens"))) if recs else None,
            "mean_cache_read": round(statistics.mean(tok("cache_read_input_tokens")))
            if recs else None,
            "mean_cache_write": round(statistics.mean(tok("cache_creation_input_tokens")))
            if recs else None,
            "mean_output": round(statistics.mean(tok("output_tokens"))) if recs else None,
            "cache_read_share_pct": pct(sum(tok("cache_read_input_tokens")), sum(total_in)),
        },
        "cost_usd": {
            "mean_per_question": round(statistics.mean(costs), 4) if costs else None,
            "total": round(sum(costs), 4) if costs else None,
        },
        "latency_s": {"p50": round(p(lat, 0.5), 2) if lat else None,
                      "p95": round(p(lat, 0.95), 2) if lat else None},
        "fallback_used": sum(r["fallback_used"] for r in recs),
    }


def run_config(name: str, settings: Settings, questions: list[dict], client, db, index,
               ex: Executor, gold_cache: dict, workers: int, archive: Path | None) -> dict:
    agent = Agent(settings, client=client, db=db, index=index)
    t0 = time.perf_counter()
    done = 0

    def one(q):
        nonlocal done
        res = agent.ask(q["question"])
        rec = score(q, res, ex, gold_cache)
        done += 1
        mark = "ok " if rec["correct"] else "BAD"
        print(f"  [{name}] {done:>2}/{len(questions)} {mark} {q['id']} "
              f"{res.status:<9} {res.latency_ms / 1000:5.1f}s {rec['match_reason'][:60]}",
              flush=True)
        return rec, res

    with ThreadPoolExecutor(max_workers=workers) as pool:
        out = list(pool.map(one, questions))
    recs = [r for r, _ in out]
    if archive is not None:
        archive.mkdir(parents=True, exist_ok=True)
        with (archive / f"{name}.jsonl").open("w") as f:
            for rec, res in out:
                f.write(json.dumps({**rec, "trace": res.trace, "rows": res.rows[:50],
                                    "columns": res.columns}, default=to_jsonable) + "\n")
    summary = summarize(recs)
    summary["wall_s"] = round(time.perf_counter() - t0, 1)
    summary["settings"] = {"model": settings.model, "effort": settings.effort,
                           "schema_retrieval": settings.schema_retrieval,
                           "fallback": settings.use_fallback, "max_turns": settings.max_turns,
                           "max_sql_errors": settings.max_sql_errors}
    return {"summary": summary, "questions": recs}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--configs", default="main,effort_low,effort_high,no_retrieval")
    ap.add_argument("--models", default="", help="comma list: run the main config per model")
    ap.add_argument("--client", choices=["anthropic", "mock"], default="anthropic")
    ap.add_argument("--seed", action="store_true", help="use the seed warehouse")
    ap.add_argument("--ids", default="", help="comma list of question ids")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--readme", action="store_true", help="update the README results block")
    a = ap.parse_args(argv)

    settings = seed_settings() if a.seed else load_settings()
    questions = load_questions()
    if a.ids:
        keep = set(a.ids.split(","))
        questions = [q for q in questions if q["id"] in keep]
    if a.limit:
        questions = questions[: a.limit]
    configs = [c for c in a.configs.split(",") if c]
    unknown = set(configs) - set(CONFIGS)
    if unknown:
        ap.error(f"unknown configs {unknown}; choose from {list(CONFIGS)}")

    if a.client == "anthropic":
        import anthropic

        client = anthropic.Anthropic(max_retries=6)
        if not client.api_key and not client.auth_token:
            print("ANTHROPIC_API_KEY is not set (put it in .env). For an offline pipeline "
                  "check run: python -m eval.run --client mock --seed", file=sys.stderr)
            return 2
    else:
        client = GoldReplayClient(questions)

    schemas = ("marts", "staging") if settings.expose_staging else ("marts",)
    db = ReadOnlyDB(settings.warehouse_path, schemas, settings.query_timeout_s)
    index = SchemaIndex.build(settings.dbt_target_dir, db, expose_staging=settings.expose_staging)
    ex = Executor(db, index)
    gold_cache: dict = {}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive = RESULTS / "runs" / stamp

    runs: dict[str, dict] = {}
    for name in configs:
        print(f"== config {name}", flush=True)
        runs[name] = run_config(name, settings.with_(**CONFIGS[name]), questions, client, db,
                                index, ex, gold_cache, a.workers, archive)
    per_model = {}
    for model in [m for m in a.models.split(",") if m]:
        if model == settings.model and "main" in runs:
            per_model[model] = runs["main"]["summary"]
            continue
        print(f"== model {model}", flush=True)
        per_model[model] = run_config(f"model_{model}", settings.with_(model=model), questions,
                                      client, db, index, ex, gold_cache, a.workers,
                                      archive)["summary"]

    counts = {b: sum(1 for q in questions if (("unanswerable" if q["unanswerable"]
                                               else q["difficulty"]) == b))
              for b in ("easy", "medium", "hard", "unanswerable")}
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "client": "anthropic" if a.client == "anthropic" else "mock-gold-replay",
        "warehouse": "seed" if settings.using_seed else "platform",
        "warehouse_path": str(settings.warehouse_path),
        "model": settings.model,
        "questions": {"total": len(questions), **counts,
                      "verified_by_jay": sum(q.get("verified_by") == "jay" for q in questions)},
        "configs": {k: v["summary"] for k, v in runs.items()},
        "per_model": per_model,
        "retrieval_probes": probe_scores(index),
        "guard_tests": guard_scores(Toolbox(index, db)),
        "per_question": runs[configs[0]]["questions"] if configs else [],
        "archive": str(archive.relative_to(ROOT)),
    }
    paths = write_reports(report, real=a.client == "anthropic", readme=a.readme)
    for pth in paths:
        print(f"wrote {pth}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
