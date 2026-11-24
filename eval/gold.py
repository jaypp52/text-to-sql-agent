"""Run every gold SQL on the warehouse and record the results (eval/gold_results.json).

    python -m eval.gold            # platform warehouse
    python -m eval.gold --seed     # seed warehouse (prints only, does not overwrite)

The file is the evidence that each gold query was run: its row count, columns, the first
rows and a hash of the full result. `make verify-questions` shows the same output while
Jay checks each item by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import json

from agent.config import ROOT, load_settings, seed_settings
from agent.db import ReadOnlyDB, to_jsonable
from agent.schema_index import SchemaIndex
from eval.run import Executor, load_questions

OUT = ROOT / "eval" / "gold_results.json"


def run_gold(seed: bool = False) -> dict:
    s = seed_settings() if seed else load_settings()
    db = ReadOnlyDB(s.warehouse_path)
    ex = Executor(db, SchemaIndex.build(s.dbt_target_dir, db))
    out = {"warehouse": "seed" if s.using_seed else "platform", "results": {}}
    for q in load_questions():
        if q["unanswerable"]:
            continue
        cols, rows = ex.run(q["gold_sql"])
        js = [[to_jsonable(v) for v in r] for r in rows]
        digest = hashlib.sha256(json.dumps(sorted(map(str, js))).encode()).hexdigest()[:16]
        out["results"][q["id"]] = {"columns": cols, "row_count": len(rows), "rows": js[:20],
                                   "sha256_16": digest}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    a = ap.parse_args()
    res = run_gold(a.seed)
    for qid, r in res["results"].items():
        print(f"{qid}: {r['row_count']} rows {r['columns']} {str(r['rows'][:3])[:100]}")
    if res["warehouse"] == "platform":
        OUT.write_text(json.dumps(res, indent=1) + "\n")
        print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
