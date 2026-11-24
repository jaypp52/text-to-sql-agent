"""Write latest.json, the Markdown table, failures.md and (optionally) the README block."""

from __future__ import annotations

import json
import re
from pathlib import Path

from agent.config import ROOT

RESULTS = ROOT / "eval" / "results"
NOTES = ROOT / "eval" / "failure_notes.json"
README = ROOT / "README.md"
START, END = "<!-- results:start -->", "<!-- results:end -->"


def _f(v, suffix: str = "", nd: int | None = None) -> str:
    if v is None:
        return "–"
    if nd is not None and isinstance(v, float):
        v = round(v, nd)
    return f"{v}{suffix}"


def _money(v) -> str:
    return "–" if v is None else f"${v:.4f}"


ROWS = [
    ("Execution accuracy, all questions", lambda s: _f(s["execution_accuracy_pct"], "%")),
    ("… answerable questions", lambda s: _f(s["answerable_accuracy_pct"], "%")),
    ("… easy", lambda s: _f(s["by_difficulty"]["easy"]["accuracy_pct"], "%")),
    ("… medium", lambda s: _f(s["by_difficulty"]["medium"]["accuracy_pct"], "%")),
    ("… hard", lambda s: _f(s["by_difficulty"]["hard"]["accuracy_pct"], "%")),
    ("Abstention accuracy (unanswerable)", lambda s: _f(s["abstention_accuracy_pct"], "%")),
    ("False abstentions (answerable)", lambda s: _f(s["false_abstentions"])),
    ("Valid SQL, first attempt", lambda s: _f(s["valid_sql_first_attempt_pct"], "%")),
    ("Valid SQL, after self-correction", lambda s: _f(s["valid_sql_final_pct"], "%")),
    ("Questions that hit ≥ 1 SQL error", lambda s: _f(s["self_correction"]["needed_retry_pct"], "%")),
    ("… of those recovered", lambda s: _f(s["self_correction"]["recovered_pct"], "%")),
    ("Mean retries per question", lambda s: _f(s["self_correction"]["mean_retries"])),
    ("Guard rejections", lambda s: _f(s["guard_rejections"])),
    ("Mean turns / tool calls",
     lambda s: f"{_f(s['mean_turns'])} / {_f(s['mean_tool_calls'])}"),
    ("Mean input tokens (total)", lambda s: _f(s["tokens"]["mean_input_total"])),
    ("… served from cache", lambda s: _f(s["tokens"]["cache_read_share_pct"], "%")),
    ("Mean output tokens", lambda s: _f(s["tokens"]["mean_output"])),
    ("Cost per question", lambda s: _money(s["cost_usd"]["mean_per_question"])),
    ("Latency p50 / p95", lambda s: f"{_f(s['latency_s']['p50'], ' s')} / "
                                    f"{_f(s['latency_s']['p95'], ' s')}"),
]

LABELS = {"main": "main", "effort_low": "effort low", "effort_high": "effort high",
          "no_retrieval": "no retrieval (all schemas in prompt)"}


def table(report: dict) -> str:
    cfgs = report["configs"]
    names = list(cfgs)
    head = ["Metric"] + [LABELS.get(n, n) + (f" ({cfgs[n]['settings']['effort']})"
                                            if n in ("main", "no_retrieval") else "")
                         for n in names]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for label, fn in ROWS:
        lines.append("| " + " | ".join([label] + [fn(cfgs[n]) for n in names]) + " |")
    return "\n".join(lines)


def per_model_table(report: dict) -> str:
    pm = report.get("per_model") or {}
    if not pm:
        return ""
    lines = ["| Model | Accuracy | Hard | Abstention | $/question | p50 latency |",
             "|---|---|---|---|---|---|"]
    for m, s in pm.items():
        lines.append(f"| {m} | {_f(s['execution_accuracy_pct'], '%')} | "
                     f"{_f(s['by_difficulty']['hard']['accuracy_pct'], '%')} | "
                     f"{_f(s['abstention_accuracy_pct'], '%')} | "
                     f"{_money(s['cost_usd']['mean_per_question'])} | "
                     f"{_f(s['latency_s']['p50'], ' s')} |")
    return "\n".join(lines)


def markdown(report: dict) -> str:
    q = report["questions"]
    main = next(iter(report["configs"].values()), None)
    parts = [
        f"Generated {report['generated_at']} by `make eval` · client **{report['client']}** · "
        f"model `{report['model']}` · warehouse **{report['warehouse']}** · "
        f"{q['total']} labelled questions ({q['easy']} easy / {q['medium']} medium / "
        f"{q['hard']} hard / {q['unanswerable']} unanswerable; "
        f"{q['verified_by_jay']} verified by Jay)",
        "",
        table(report),
    ]
    gt = report.get("guard_tests")
    if gt:
        parts += ["", f"Guardrail attack list: {gt['attacks_blocked']}/{gt['attacks']} attacks "
                      f"blocked, {gt['legit_passed']}/{gt['legit']} legitimate queries allowed; "
                      f"{gt['lockdown_blocked_without_guard']}/{gt['lockdown']} file, extension "
                      "and config attacks also stopped by the locked connection with the guard "
                      "bypassed."]
    rp = report.get("retrieval_probes")
    if rp:
        parts += ["", f"Schema retrieval (BM25) on {rp['n']} probe queries: right table ranked "
                      f"first {rp['top1']}/{rp['n']}, in the top 3 {rp['top3']}/{rp['n']}."]
    pmt = per_model_table(report)
    if pmt:
        parts += ["", "Per-model comparison (main config):", "", pmt]
    if main:
        sc = main["self_correction"]
        parts += [
            "",
            "Resume numbers (main config): "
            f"execution accuracy {_f(main['execution_accuracy_pct'], '%')} overall "
            f"({_f(main['by_difficulty']['hard']['accuracy_pct'], '%')} hard); "
            f"{_f(sc['needed_retry_pct'], '%')} of questions hit an SQL error and "
            f"{_f(sc['recovered_pct'], '%')} of those recovered; "
            f"{main['guard_rejections']} guard rejections on the eval set; "
            f"{_money(main['cost_usd']['mean_per_question'])} per question; "
            f"p50 latency {_f(main['latency_s']['p50'], ' s')}.",
        ]
    return "\n".join(parts) + "\n"


def cause(rec: dict) -> str:
    if rec["bucket"] == "unanswerable":
        return rec["match_reason"]
    if rec["status"] == "error":
        return f"did not finish ({rec['error_kind']})"
    if rec["status"] == "abstained":
        return "false abstention"
    reason = rec.get("match_reason", "")
    if reason.startswith("row count differs"):
        return "row count differs: filter, date window or grain"
    if "columns" in reason:
        return "missing columns"
    if reason.startswith("predicted SQL failed"):
        return "SQL failed on re-run"
    return "values differ: metric definition, join or aggregation"


def failures(report: dict) -> str:
    notes = json.loads(NOTES.read_text()) if NOTES.exists() else {}
    bad = [r for r in report["per_question"] if not r["correct"]]
    out = [
        "# Failure analysis",
        "",
        f"Generated {report['generated_at']} from the `{next(iter(report['configs']), '?')}` "
        f"config ({report['client']}, `{report['model']}`, {report['warehouse']} warehouse). "
        f"{len(bad)} of {len(report['per_question'])} answers were wrong.",
        "",
        "The *auto* cause comes from the comparison; the *note* column is a hand-written "
        "diagnosis from `eval/failure_notes.json` (add one per id after reading the trace in "
        f"`{report['archive']}`).",
        "",
    ]
    if not bad:
        out.append("No wrong answers in this run.")
    tally: dict[str, int] = {}
    for r in bad:
        c = notes.get(r["id"], {}).get("cause") or cause(r)
        tally[c] = tally.get(c, 0) + 1
    if tally:
        out += ["| Cause | Count |", "|---|---|"]
        out += [f"| {c} | {n} |" for c, n in sorted(tally.items(), key=lambda x: -x[1])]
        out.append("")
    for r in bad:
        note = notes.get(r["id"], {})
        out += [
            f"## {r['id']} ({r['bucket']}): {r['question']}",
            "",
            f"- auto cause: {cause(r)}",
            f"- note: {note.get('note', '_not yet annotated_')}",
            f"- status: {r['status']}; SQL errors: {r['sql_errors']}; turns: {r['turns']}",
            f"- answer: {r['answer'][:400] or '–'}",
            "",
            "Gold SQL:",
            "```sql",
            r["gold_sql"] or "-- (unanswerable: no SQL)",
            "```",
            "Predicted SQL:",
            "```sql",
            r["pred_sql"] or "-- (empty)",
            "```",
            "",
        ]
    return "\n".join(out) + "\n"


def update_readme(md: str) -> bool:
    if not README.exists():
        return False
    text = README.read_text()
    if START not in text or END not in text:
        return False
    new = re.sub(re.escape(START) + r".*?" + re.escape(END),
                 lambda _: f"{START}\n{md}{END}", text, flags=re.DOTALL)
    README.write_text(new)
    return True


def write_reports(report: dict, real: bool, readme: bool = False) -> list[Path]:
    RESULTS.mkdir(parents=True, exist_ok=True)
    md = markdown(report)
    if real:
        paths = [RESULTS / "latest.json", RESULTS / "latest.md", RESULTS / "failures.md"]
        paths[0].write_text(json.dumps(report, indent=1, default=str) + "\n")
        paths[1].write_text("# Eval results\n\n" + md)
        paths[2].write_text(failures(report))
        if readme and update_readme(md):
            paths.append(README)
        return paths
    stem = f"mock_{report['warehouse']}"
    paths = [RESULTS / f"{stem}.json", RESULTS / f"{stem}.md"]
    paths[0].write_text(json.dumps(report, indent=1, default=str) + "\n")
    paths[1].write_text("# Pipeline check (mock client, not model accuracy)\n\n" + md)
    return paths
