"""Command line.

    python -m agent.cli ask "Which firmware has the highest crash rate?" --trace
    python -m agent.cli schema "crash rate by firmware"      # what search_schema returns
    python -m agent.cli sql "SELECT count(*) FROM fct_trip"  # run SQL through the guard
    python -m agent.cli demo                                 # 5 sample questions -> docs/
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from agent.config import EFFORTS, ROOT, load_settings, seed_settings
from agent.db import ReadOnlyDB
from agent.schema_index import SchemaIndex
from agent.tools import Toolbox

DEMO_QUESTIONS = [
    "Which firmware version has the highest crash rate per 100 device-days in the first "
    "week of January 2026?",
    "Did TX-200 devices crash more after upgrading to 1.1.0?",
    "Which region loses LTE connectivity most often?",
    "How many devices were on firmware 1.1.0 at midnight UTC on 2026-01-05?",
    "What were the five longest complete trips, and which devices drove them?",
]


def table(columns: list[str], rows: list[list], max_rows: int = 20, width: int = 28) -> str:
    if not columns:
        return "(no rows)"
    cells = [[("NULL" if v is None else str(v))[:width] for v in r] for r in rows[:max_rows]]
    w = [max(len(c), *(len(r[i]) for r in cells)) if cells else len(c)
         for i, c in enumerate(columns)]
    line = "  ".join(c.ljust(w[i]) for i, c in enumerate(columns))
    out = [line, "  ".join("-" * x for x in w)]
    out += ["  ".join(v.ljust(w[i]) for i, v in enumerate(r)) for r in cells]
    if len(rows) > max_rows:
        out.append(f"... {len(rows) - max_rows} more rows")
    return "\n".join(out)


def render(res, trace: bool) -> str:
    out = []
    if trace:
        out.append("── trace " + "─" * 60)
        for t in res.trace:
            if t["type"] == "api":
                u = t["usage"]
                out.append(f"[turn {t['turn']}] API {t['stop_reason']} {t['ms']:.0f} ms "
                           f"(in {u['input_tokens']}, cache read {u['cache_read_input_tokens']}, "
                           f"cache write {u['cache_creation_input_tokens']}, out {u['output_tokens']})")
            elif t["type"] == "text":
                out.append(f"  model: {t['text']}")
            elif t["type"] in ("tool_use", "submit"):
                flag = " ERROR" if t.get("is_error") else ""
                out.append(f"  → {t['tool']}({json.dumps(t['input'], ensure_ascii=False)[:500]})"
                           f"{flag}")
                if t.get("output"):
                    out.append(f"    ← {t['output'][:500]}")
            elif t["type"] == "nudge":
                out.append(f"  (nudge) {t['text']}")
        out.append("─" * 69)
    out.append(f"status: {res.status}" + (f" ({res.error_kind}: {res.error})" if res.error else ""))
    out.append(f"answer: {res.answer}")
    if res.assumptions:
        out.append("assumptions:\n" + "\n".join(f"  - {a}" for a in res.assumptions))
    if res.sql:
        out.append("sql:\n" + "\n".join("  " + line for line in res.sql.splitlines()))
        out.append(table(res.columns, res.rows))
    cost = f"${res.cost_usd:.4f}" if res.cost_usd is not None else "n/a"
    out.append(f"turns {res.turns}, tool calls {res.tool_calls}, SQL errors {res.sql_errors}, "
               f"tokens in {res.usage.get('input_tokens', 0)}+"
               f"{res.usage.get('cache_read_input_tokens', 0)} cached, "
               f"out {res.usage.get('output_tokens', 0)}, cost {cost}, "
               f"{res.latency_ms / 1000:.1f} s")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("ask", "demo"):
        p = sub.add_parser(name)
        if name == "ask":
            p.add_argument("question")
            p.add_argument("--trace", action="store_true")
            p.add_argument("--json", action="store_true")
        p.add_argument("--effort", choices=EFFORTS)
        p.add_argument("--model")
        p.add_argument("--no-retrieval", action="store_true")
        p.add_argument("--seed", action="store_true", help="use the seed warehouse")
    p = sub.add_parser("schema")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--seed", action="store_true")
    p = sub.add_parser("sql")
    p.add_argument("sql")
    p.add_argument("--seed", action="store_true")
    a = ap.parse_args(argv)

    s = seed_settings() if a.seed else load_settings()
    if a.cmd in ("schema", "sql"):
        db = ReadOnlyDB(s.warehouse_path)
        index = SchemaIndex.build(s.dbt_target_dir, db)
        if a.cmd == "schema":
            print(json.dumps(index.search(a.query, a.k), indent=1, ensure_ascii=False))
            return 0
        out = Toolbox(index, db).run_sql(a.sql)
        if out.is_error:
            print(f"{out.kind}: {json.loads(out.content)['error']}", file=sys.stderr)
            return 1
        print(table(out.data["columns"], out.data["rows"], max_rows=50))
        print(f"({out.data['row_count']} rows, {out.data['db_ms']:.0f} ms"
              f"{', LIMIT injected' if out.data['limit_injected'] else ''})")
        return 0

    from agent.loop import Agent

    over = {}
    if a.effort:
        over["effort"] = a.effort
    if a.model:
        over["model"] = a.model
    if a.no_retrieval:
        over["schema_retrieval"] = False
    s = s.with_(**over)
    agent = Agent(s)
    if not agent.client.api_key and not agent.client.auth_token:
        print("ANTHROPIC_API_KEY is not set; put it in .env (see .env.example).", file=sys.stderr)
        return 2
    if a.cmd == "ask":
        res = agent.ask(a.question)
        if a.json:
            print(json.dumps(res.to_dict(), indent=1, default=str, ensure_ascii=False))
        else:
            print(render(res, a.trace))
        return 0 if res.status != "error" else 1

    # demo: run the sample questions and keep the traces as a document
    lines = [f"# Sample runs\n\nGenerated {datetime.now(UTC).isoformat(timespec='seconds')} "
             f"with `python -m agent.cli demo` (model `{s.model}`, effort `{s.effort}`, "
             f"warehouse `{'seed' if s.using_seed else 'platform'}`).\n"]
    ok = 0
    for i, q in enumerate(DEMO_QUESTIONS, 1):
        res = agent.ask(q)
        ok += res.status != "error"
        text = render(res, trace=True)
        print(f"\n### {i}. {q}\n{text}")
        lines.append(f"## {i}. {q}\n\n```text\n{text}\n```\n")
    path = ROOT / "docs" / "sample_runs.md"
    path.write_text("\n".join(lines))
    print(f"\nwrote {path} ({ok}/{len(DEMO_QUESTIONS)} answered)")
    return 0 if ok == len(DEMO_QUESTIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
