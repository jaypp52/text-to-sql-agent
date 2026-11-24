"""Hand-check the labelled set: shows each question, its gold SQL and the result on the
platform warehouse, and records verified_by="jay" for the items you accept.

    python -m eval.verify            # pending items only
    python -m eval.verify --all      # re-check everything

Answers: y = correct, n = wrong (leave pending and write a note), s = skip, q = quit.
Verification survives `python -m eval.questions_src` as long as the gold SQL is unchanged.
"""

from __future__ import annotations

import argparse
import json

from agent.cli import table
from agent.config import load_settings
from agent.db import ReadOnlyDB
from agent.schema_index import SchemaIndex
from eval.run import QUESTIONS, Executor, load_questions


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--who", default="jay")
    a = ap.parse_args()
    s = load_settings()
    if s.using_seed:
        raise SystemExit("verify against the platform warehouse, not the seed")
    db = ReadOnlyDB(s.warehouse_path)
    ex = Executor(db, SchemaIndex.build(s.dbt_target_dir, db))
    qs = load_questions()
    notes = []
    for q in qs:
        if q.get("verified_by") == a.who and not a.all:
            continue
        print("\n" + "=" * 78)
        print(f"{q['id']} [{q['difficulty']}{', unanswerable' if q['unanswerable'] else ''}] "
              f"{q['question']}")
        if q["unanswerable"]:
            print("gold: (unanswerable - the agent should submit empty SQL and say so)")
        else:
            print("gold SQL:\n  " + q["gold_sql"])
            cols, rows = ex.run(q["gold_sql"])
            print(table(cols, [list(r) for r in rows], max_rows=15))
            print(f"({len(rows)} rows; order_matters={q['order_matters']})")
        ans = input("correct? [y/n/s/q] ").strip().lower()
        if ans == "q":
            break
        if ans == "y":
            q["verified_by"] = a.who
        elif ans == "n":
            notes.append((q["id"], input("what is wrong? ")))
    QUESTIONS.write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in qs))
    done = sum(q.get("verified_by") == a.who for q in qs)
    print(f"\n{done}/{len(qs)} verified by {a.who}; wrote {QUESTIONS}")
    for qid, n in notes:
        print(f"  fix {qid}: {n}  (edit eval/questions_src.py, then python -m eval.questions_src)")


if __name__ == "__main__":
    main()
