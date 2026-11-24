"""Copy the parts of the platform's dbt docs the agent uses into seed/dbt_target/.

    python -m seed.export_dbt_docs [--from ../telemetry-data-platform/dbt/target]

The trimmed manifest.json/catalog.json are committed, so CI and anyone without the
telemetry platform get the same schema documentation. Re-run after changing the
platform's dbt models.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.config import DEFAULT_DBT_TARGET, SEED_DBT_TARGET

NODE_KEYS = ("name", "resource_type", "schema", "description", "columns", "primary_key",
             "fqn", "test_metadata", "attached_node", "refs", "unique_id")


def trim(target: Path) -> tuple[dict, dict]:
    manifest = json.loads((target / "manifest.json").read_text())
    catalog = json.loads((target / "catalog.json").read_text())
    nodes = {}
    for uid, n in manifest["nodes"].items():
        if n["resource_type"] not in ("model", "seed", "snapshot", "test"):
            continue
        if n["resource_type"] == "test" and not n.get("attached_node"):
            continue
        keep = {k: n[k] for k in NODE_KEYS if k in n}
        if "columns" in keep:
            keep["columns"] = {c: {"name": v["name"], "description": v.get("description", "")}
                               for c, v in keep["columns"].items()}
        nodes[uid] = keep
    m = {"metadata": {k: manifest["metadata"].get(k) for k in ("dbt_version", "generated_at",
                                                               "project_name")},
         "nodes": nodes}
    c = {"metadata": {k: catalog["metadata"].get(k) for k in ("dbt_version", "generated_at")},
         "nodes": {uid: {"metadata": {k: v["metadata"][k] for k in ("schema", "name", "type")},
                         "columns": {cn: {k: cv[k] for k in ("name", "type", "index")}
                                     for cn, cv in v["columns"].items()}}
                   for uid, v in catalog["nodes"].items()}}
    return m, c


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", default=str(DEFAULT_DBT_TARGET))
    ap.add_argument("--to", default=str(SEED_DBT_TARGET))
    a = ap.parse_args()
    m, c = trim(Path(a.src))
    out = Path(a.to)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(m, indent=1, sort_keys=True) + "\n")
    (out / "catalog.json").write_text(json.dumps(c, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out}/manifest.json ({len(m['nodes'])} nodes) and catalog.json "
          f"({len(c['nodes'])} relations)")


if __name__ == "__main__":
    main()
