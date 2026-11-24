"""Score a Claude Code baseline run of the labelled set.

The baseline is NOT the API agent (agent/loop.py). It is Claude, driven through Claude
Code sessions that had only the agent's own tools as a command line (schema search,
describe table, guarded run_sql, submit) in a copy of the repo without eval/ (no gold
SQL), with the agent's system prompt as instructions. Every SQL call and submit was
logged by the tool script itself, so the self-correction numbers are measured, not
self-reported. Tokens, cost and latency are not available for these runs.

    python -m eval.claude_code_baseline --run haiku=<dir> --run sonnet=<dir> --run opus=<dir> [--readme]

Each <dir> holds out/*.jsonl (submitted answers) and logs/<id>.jsonl (tool-call logs).
Writes eval/results/claude_code_baseline.json and .md, plus a README block between the
`baseline:start` / `baseline:end` markers with --readme.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from agent.config import ROOT, load_settings
from agent.db import ReadOnlyDB
from agent.schema_index import SchemaIndex
from eval.compare import compare, is_abstention
from eval.run import Executor, load_questions, pct

RESULTS = ROOT / "eval" / "results"
README = ROOT / "README.md"
START, END = "<!-- baseline:start -->", "<!-- baseline:end -->"


def load_run(d: Path) -> tuple[dict, dict]:
    answers, logs = {}, {}
    for f in sorted((d / "out").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if line.strip():
                a = json.loads(line)
                answers[a["id"]] = a  # last submit wins
    for f in (d / "logs").glob("*.jsonl"):
        logs[f.stem] = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    return answers, logs


def score_run(questions, answers, logs, ex: Executor, gold: dict) -> dict:
    recs = []
    for q in questions:
        a = answers.get(q["id"])
        attempts = [x for x in logs.get(q["id"], []) if x["kind"] in ("run_sql", "submit")]
        errs = [x for x in attempts if not x["ok"]]
        retries = sum(1 for i in range(1, len(attempts)) if not attempts[i - 1]["ok"])
        rec = {"id": q["id"], "bucket": "unanswerable" if q["unanswerable"] else q["difficulty"],
               "question": q["question"], "gold_sql": q["gold_sql"], "submitted": a is not None,
               "pred_sql": (a or {}).get("sql", ""), "answer": (a or {}).get("answer", ""),
               "sql_attempts": len(attempts), "sql_errors": len(errs), "retries": retries,
               "guard_rejections": sum(x["result_kind"] == "guard_rejection" for x in attempts),
               "first_sql_ok": attempts[0]["ok"] if attempts else None}
        if a is None:
            rec.update(correct=False, reason="no answer submitted")
        elif q["unanswerable"]:
            rec["correct"] = is_abstention(a["sql"], a["answer"])
            rec["reason"] = "abstained" if rec["correct"] else (
                "answered an unanswerable question" if a["sql"] else "empty SQL, answer did not abstain")
        elif not a["sql"].strip():
            rec.update(correct=False, reason="false abstention")
        else:
            if q["id"] not in gold:
                gold[q["id"]] = ex.run(q["gold_sql"])[1]
            try:
                _, rows = ex.run(a["sql"])
                m = compare(gold[q["id"]], rows, q.get("order_matters", False))
                rec.update(correct=m.ok, reason=m.reason or "match")
            except Exception as e:  # noqa: BLE001
                rec.update(correct=False, reason=f"SQL failed on re-run: {str(e)[:120]}")
        recs.append(rec)
    return {"summary": summarize(recs), "questions": recs}


def summarize(recs: list[dict]) -> dict:
    ans = [r for r in recs if r["bucket"] != "unanswerable"]
    una = [r for r in recs if r["bucket"] == "unanswerable"]
    with_sql = [r for r in recs if r["sql_attempts"] > 0]
    errored = [r for r in with_sql if r["sql_errors"] > 0]
    recovered = [r for r in errored if r["submitted"] and r["pred_sql"]]
    return {
        "n": len(recs), "submitted": sum(r["submitted"] for r in recs),
        "execution_accuracy_pct": pct(sum(r["correct"] for r in recs), len(recs)),
        "answerable_accuracy_pct": pct(sum(r["correct"] for r in ans), len(ans)),
        "by_difficulty": {b: {"n": sum(r["bucket"] == b for r in ans),
                              "accuracy_pct": pct(sum(r["correct"] for r in ans if r["bucket"] == b),
                                                  sum(r["bucket"] == b for r in ans))}
                          for b in ("easy", "medium", "hard")},
        "abstention_accuracy_pct": pct(sum(r["correct"] for r in una), len(una)),
        "false_abstentions": sum(r["reason"] == "false abstention" for r in ans),
        "valid_sql_first_attempt_pct": pct(sum(bool(r["first_sql_ok"]) for r in with_sql), len(with_sql)),
        "needed_retry_pct": pct(len(errored), len(with_sql)),
        "recovered_pct": pct(len(recovered), len(errored)),
        "mean_sql_attempts": round(sum(r["sql_attempts"] for r in with_sql) / len(with_sql), 2)
        if with_sql else None,
        "guard_rejections": sum(r["guard_rejections"] for r in recs),
    }


NOTES = ROOT / "eval" / "claude_code_baseline_notes.json"


def _p(v) -> str:
    return "n/a" if v is None else f"{v}%"


def markdown(report: dict) -> str:
    runs = report["runs"]
    names = list(runs)
    rows = [
        ("Execution accuracy, all 50", lambda s: _p(s['execution_accuracy_pct'])),
        ("… answerable (44)", lambda s: _p(s['answerable_accuracy_pct'])),
        ("… easy", lambda s: _p(s['by_difficulty']['easy']['accuracy_pct'])),
        ("… medium", lambda s: _p(s['by_difficulty']['medium']['accuracy_pct'])),
        ("… hard", lambda s: _p(s['by_difficulty']['hard']['accuracy_pct'])),
        ("Abstention accuracy (6 unanswerable)", lambda s: _p(s['abstention_accuracy_pct'])),
        ("Valid SQL, first attempt", lambda s: _p(s['valid_sql_first_attempt_pct'])),
        ("Questions that hit ≥ 1 SQL error", lambda s: _p(s['needed_retry_pct'])),
        ("… of those recovered", lambda s: _p(s['recovered_pct'])),
        ("Mean SQL attempts per question", lambda s: f"{s['mean_sql_attempts']}"),
        ("Guard rejections", lambda s: f"{s['guard_rejections']}"),
    ]
    out = [
        f"Generated {report['generated_at']} by `python -m eval.claude_code_baseline` · "
        f"{report['questions']} labelled questions · platform warehouse · "
        "**Claude via Claude Code sessions using this agent's tools, not the API agent loop** "
        "(no token, cost or latency figures).",
        "",
        "| Metric | " + " | ".join(f"Claude Code · {n}" for n in names) + " |",
        "|---|" + "---|" * len(names),
    ]
    for label, fn in rows:
        out.append(f"| {label} | " + " | ".join(fn(runs[n]["summary"]) for n in names) + " |")
    notes = json.loads(NOTES.read_text()) if NOTES.exists() else {}
    wrong: dict[str, list[str]] = {}
    for n in names:
        for q in runs[n]["questions"]:
            if not q["correct"]:
                wrong.setdefault(q["id"], []).append(n)
    if wrong:
        out += ["", "Every wrong answer, with a hand-written diagnosis (`eval/claude_code_baseline_notes.json`):",
                "", "| Question | Wrong for | Cause |", "|---|---|---|"]
        for qid in sorted(wrong):
            out.append(f"| {qid} | {', '.join(wrong[qid])} | "
                       f"{notes.get(qid, {}).get('cause', '_not yet diagnosed_')} |")
    return "\n".join(out) + "\n"


def update_readme(md: str) -> bool:
    text = README.read_text()
    if START not in text:
        return False
    README.write_text(re.sub(re.escape(START) + r".*?" + re.escape(END),
                             lambda _: f"{START}\n{md}{END}", text, flags=re.DOTALL))
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", required=True, help="name=dir")
    ap.add_argument("--readme", action="store_true")
    a = ap.parse_args()
    s = load_settings()
    db = ReadOnlyDB(s.warehouse_path)
    ex = Executor(db, SchemaIndex.build(s.dbt_target_dir, db))
    qs = load_questions()
    gold: dict = {}
    runs = {}
    for spec in a.run:
        name, d = spec.split("=", 1)
        answers, logs = load_run(Path(d))
        runs[name] = score_run(qs, answers, logs, ex, gold)
        sm = runs[name]["summary"]
        print(f"{name}: {sm['submitted']}/{sm['n']} submitted, accuracy {sm['execution_accuracy_pct']}%")
    report = {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
              "kind": "claude-code-baseline", "warehouse": "platform", "questions": len(qs),
              "verified_by_jay": sum(q.get("verified_by") == "jay" for q in qs), "runs": runs}
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "claude_code_baseline.json").write_text(json.dumps(report, indent=1, default=str) + "\n")
    md = markdown(report)
    (RESULTS / "claude_code_baseline.md").write_text("# Claude Code baseline\n\n" + md)
    print("wrote eval/results/claude_code_baseline.json and .md")
    if a.readme and update_readme(md):
        print("updated README")


if __name__ == "__main__":
    main()
