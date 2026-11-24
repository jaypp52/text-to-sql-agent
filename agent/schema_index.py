"""Schema index: one document per dbt model, searched with BM25.

Built from the platform's ``target/manifest.json`` (descriptions, primary keys,
``relationships`` tests) and ``target/catalog.json`` (column types as DuckDB stores them),
plus 3 sample rows, the row count and date coverage read through the read-only
connection. Only mart models (``dim_*``, ``fct_*``) are exposed by default.

Why BM25 and not embeddings: the governed layer is 10 tables and ~110 columns whose names
and descriptions share the user's vocabulary (crash, firmware, LTE, trip). Lexical
retrieval ranks the right model first on every probe in tests/test_schema_index.py and
keeps the index dependency-free and deterministic.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rank_bm25 import BM25Okapi

from agent.db import ReadOnlyDB, to_jsonable

TOKEN = re.compile(r"[a-z0-9]+")
STOP = {
    "the", "a", "an", "of", "in", "on", "by", "for", "per", "to", "and", "or", "is", "are",
    "was", "were", "with", "what", "which", "how", "many", "much", "each", "from", "at", "as",
    "be", "that", "this", "it", "its", "one", "row", "rows", "show", "me", "list", "give",
    "did", "do", "does", "has", "have", "when", "who", "most", "all", "there", "than",
}
# A few domain synonyms so the user's words reach the dbt vocabulary.
SYNONYMS = {
    "crashes": "crash", "crashed": "crash", "crashing": "crash",
    "connectivity": "lte", "network": "lte", "signal": "lte rsrp sinr", "modem": "lte",
    "upgrade": "firmware", "upgraded": "firmware", "upgrading": "firmware", "fw": "firmware",
    "version": "firmware version", "versions": "firmware version", "ota": "firmware",
    "vehicle": "device", "vehicles": "device", "camera": "device crash", "cameras": "device",
    "model": "hardware_model hardware", "models": "hardware_model hardware",
    "temperature": "temp", "hot": "temp overheat", "heat": "temp overheat",
    "distance": "distance_km km", "km": "distance_km", "drove": "drive trip",
    "driving": "drive drive_minutes", "journeys": "trip", "journey": "trip", "trips": "trip",
    "gps": "gps fix_valid", "location": "lat lon gps", "speed": "speed_kmh",
    "day": "date event_date daily", "daily": "date event_date day", "days": "daily date",
    "percentage": "pct", "percent": "pct", "share": "pct", "rate": "rate pct", "week": "date", "weekend":
    "is_weekend date", "alarm": "event", "alarms": "event", "events": "event",
    "region": "region_id region", "regions": "region_id region", "country": "region",
    "fleet": "device",
}


def tokenize(text: str, expand: bool = False) -> list[str]:
    out: list[str] = []
    for tok in TOKEN.findall(text.lower().replace("_", " ")):
        if tok in STOP:
            continue
        if expand and tok in SYNONYMS:
            out.extend(SYNONYMS[tok].replace("_", " ").split())
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        out.append(tok)
    return out


@dataclass
class Column:
    name: str
    type: str
    description: str = ""


@dataclass
class Join:
    column: str
    to_table: str
    to_column: str


@dataclass
class TableDoc:
    name: str
    schema: str
    layer: str  # "mart" | "staging"
    kind: str  # "model" | "seed"
    description: str
    columns: list[Column]
    primary_key: list[str] = field(default_factory=list)
    grain: list[str] = field(default_factory=list)  # unique combination of columns, if tested
    joins: list[Join] = field(default_factory=list)  # outgoing FKs (relationships tests)
    referenced_by: list[Join] = field(default_factory=list)  # incoming FKs
    row_count: int | None = None
    coverage: dict[str, list] = field(default_factory=dict)  # date column -> [min, max]
    sample_rows: list[dict] = field(default_factory=list)

    @property
    def fqn(self) -> str:
        return f"{self.schema}.{self.name}"

    def search_text(self) -> str:
        parts = [(self.name.replace("_", " ") + " ") * 3, self.description]
        for c in self.columns:  # column names count twice, descriptions once
            name = c.name.replace("_", " ")
            parts.append(f"{name} {name} {c.description}")
        return " ".join(parts)

    def summary(self, matched: list[Column] | None = None) -> dict:
        d = {
            "table": self.fqn,
            "description": _short(self.description, 400),
            "primary_key": self.primary_key,
            "grain": self.grain,
            "row_count": self.row_count,
        }
        if matched is not None:
            d["matching_columns"] = [asdict(c) for c in matched]
        if self.joins:
            d["joins"] = [f"{j.column} -> {j.to_table}.{j.to_column}" for j in self.joins]
        return d

    def full(self) -> dict:
        return {
            "table": self.fqn,
            "layer": self.layer,
            "description": self.description,
            "primary_key": self.primary_key,
            "grain": self.grain,
            "row_count": self.row_count,
            "columns": [asdict(c) for c in self.columns],
            "joins": [f"{j.column} -> {j.to_table}.{j.to_column}" for j in self.joins],
            "referenced_by": [f"{j.to_table}.{j.to_column} -> {self.name}.{j.column}"
                              for j in self.referenced_by],
            "date_coverage": self.coverage,
            "sample_rows": self.sample_rows,
        }


def _short(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _ref_name(expr: str) -> str | None:
    m = re.search(r"ref\(\s*['\"]([^'\"]+)['\"]\s*\)", expr or "")
    return m.group(1) if m else None


class SchemaIndex:
    def __init__(self, docs: list[TableDoc]):
        self.docs = docs
        self.by_name = {d.name: d for d in docs}
        self._bm25 = BM25Okapi([tokenize(d.search_text()) for d in docs])
        self._name_tokens = [set(tokenize(d.name)) for d in docs]

    # -- construction ---------------------------------------------------------------------
    @classmethod
    def build(cls, target_dir: str | Path, db: ReadOnlyDB | None = None,
              expose_staging: bool = False, sample_rows: int = 3) -> SchemaIndex:
        target = Path(target_dir)
        manifest = json.loads((target / "manifest.json").read_text())
        catalog = json.loads((target / "catalog.json").read_text())
        nodes = {k: v for k, v in manifest["nodes"].items()
                 if v.get("resource_type") in ("model", "seed")}
        cat_nodes = catalog.get("nodes", {})

        def exposed(node: dict) -> bool:
            name = node["name"]
            if name.startswith(("dim_", "fct_")):
                return True
            return expose_staging and name.startswith("stg_")

        docs: dict[str, TableDoc] = {}
        for uid, node in nodes.items():
            if not exposed(node) or uid not in cat_nodes:
                continue
            cat_cols = cat_nodes[uid]["columns"]
            man_cols = node.get("columns", {})
            cols = [
                Column(name=c["name"], type=c["type"],
                       description=_short(man_cols.get(c["name"], {}).get("description", ""), 300))
                for c in sorted(cat_cols.values(), key=lambda c: c["index"])
            ]
            name = node["name"]
            docs[name] = TableDoc(
                name=name,
                schema=cat_nodes[uid]["metadata"]["schema"],
                layer="staging" if name.startswith("stg_") else "mart",
                kind=node["resource_type"],
                description=_short(node.get("description", ""), 1200),
                columns=cols,
            )

        # unique / unique_combination_of_columns tests -> primary key and grain;
        # relationships tests -> join paths
        uniques: dict[str, list[str]] = {}
        combos: dict[str, list[str]] = {}
        for t in manifest["nodes"].values():
            meta = t.get("test_metadata") or {}
            if t.get("resource_type") != "test":
                continue
            kw = meta.get("kwargs", {})
            src = (t.get("attached_node") or "").split(".")[-1]
            if meta.get("name") == "unique" and src in docs:
                uniques.setdefault(src, []).append(kw["column_name"])
            if meta.get("name") == "unique_combination_of_columns" and src in docs:
                combos[src] = list(kw.get("combination_of_columns") or [])
            if meta.get("name") != "relationships":
                continue
            dst = _ref_name(kw.get("to", ""))
            if src in docs and dst in docs:
                j = Join(column=kw["column_name"], to_table=docs[dst].fqn, to_column=kw["field"])
                if j not in docs[src].joins:
                    docs[src].joins.append(j)
                    back = Join(column=kw["field"], to_table=docs[src].fqn,
                                to_column=kw["column_name"])
                    docs[dst].referenced_by.append(back)

        for name, d in docs.items():
            if name in uniques:
                d.primary_key = sorted(uniques[name])[:1]
            elif name in combos:
                d.primary_key = combos[name]
            d.grain = combos.get(name, d.primary_key)

        if db is not None:
            for d in docs.values():
                cls._profile(d, db, sample_rows)
        return cls(sorted(docs.values(), key=lambda d: d.name))

    @staticmethod
    def _profile(d: TableDoc, db: ReadOnlyDB, n: int) -> None:
        q = f'"{d.schema}"."{d.name}"'
        d.row_count = db.execute(f"select count(*) from {q}").rows[0][0]
        order = ", ".join(f'"{c}"' for c in d.primary_key) or "1"
        res = db.execute(f"select * from {q} order by {order} limit {n}")
        d.sample_rows = [
            {c: to_jsonable(v) for c, v in zip(res.columns, row, strict=True)} for row in res.rows
        ]
        date_cols = [c.name for c in d.columns if c.type.upper() in ("DATE", "TIMESTAMP")]
        if date_cols and d.name != "dim_date":
            sel = ", ".join(f'min("{c}"), max("{c}")' for c in date_cols)
            row = db.execute(f"select {sel} from {q}").rows[0]
            for i, c in enumerate(date_cols):
                d.coverage[c] = [to_jsonable(row[2 * i]), to_jsonable(row[2 * i + 1])]

    # -- queries --------------------------------------------------------------------------
    @property
    def allowed_tables(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for d in self.docs:
            out.setdefault(d.schema, set()).add(d.name)
        return out

    def resolve(self, table: str) -> TableDoc | None:
        name = table.strip().strip('"').lower().split(".")[-1].strip('"')
        return self.by_name.get(name)

    def search(self, query: str, k: int = 5) -> list[dict]:
        k = max(1, min(int(k), 8))
        q = tokenize(query, expand=True)
        if not q:
            return [d.summary() for d in self.docs[:k]]
        # BM25, plus one point per query word that appears in the table's own name
        qset = set(q)
        scores = [float(sc) + len(qset & self._name_tokens[i])
                  for i, sc in enumerate(self._bm25.get_scores(q))]
        ranked = sorted(range(len(self.docs)), key=lambda i: (-scores[i], self.docs[i].name))
        out = []
        for i in ranked[:k]:
            d = self.docs[i]
            by_name = [c for c in d.columns if qset & set(tokenize(c.name))]
            by_desc = [c for c in d.columns if c not in by_name
                       and qset & set(tokenize(c.description))]
            item = d.summary(matched=(by_name + by_desc)[:8])
            item["score"] = round(float(scores[i]), 3)
            out.append(item)
        return out

    def describe(self, table: str) -> dict | None:
        d = self.resolve(table)
        return d.full() if d else None

    def all_schemas_text(self) -> str:
        """Every exposed schema, for the no-retrieval ablation (pasted into the prompt)."""
        return json.dumps([d.full() for d in self.docs], indent=1, sort_keys=True, default=str)
