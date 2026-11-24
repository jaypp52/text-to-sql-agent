"""Tool definitions (JSON Schema, strict) and their Python handlers.

The model never gets a database handle: ``run_sql`` is the only path to DuckDB, and it
always goes through SqlGuard and the locked read-only connection.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import duckdb

from agent.db import QueryTimeout, ReadOnlyDB, to_jsonable
from agent.schema_index import SchemaIndex
from agent.sql_guard import GuardRejection, SqlGuard, SqlSyntaxError

SEARCH_SCHEMA = {
    "name": "search_schema",
    "description": (
        "Search the warehouse's dbt documentation for the tables relevant to a question. "
        "Returns the best-matching tables with their description, primary key, grain, "
        "row count, join keys and the columns that match the query. Use it first, then "
        "describe_table on the tables you plan to query."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keywords from the question, e.g. 'crash rate firmware'.",
            },
            "k": {
                "type": "integer",
                "enum": [1, 2, 3, 4, 5, 6, 7, 8],
                "description": "How many tables to return (1-8). 5 is a good default.",
            },
        },
        "required": ["query", "k"],
        "additionalProperties": False,
    },
}

DESCRIBE_TABLE = {
    "name": "describe_table",
    "description": (
        "Full documentation of one table: every column with its type and dbt description "
        "(metric definitions live here), primary key, grain, join keys, the date range the "
        "data covers, and 3 sample rows."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Table name, e.g. 'fct_trip'."},
        },
        "required": ["table"],
        "additionalProperties": False,
    },
}

RUN_SQL = {
    "name": "run_sql",
    "description": (
        "Execute ONE read-only DuckDB SELECT against the warehouse and return at most 50 "
        "rows. Only documented tables may be queried; file/URL functions, DDL/DML, SET, "
        "PRAGMA and multiple statements are rejected. LIMIT 1000 is added when the query "
        "has none; queries time out after 10 s. Errors come back as tool errors - read "
        "them, fix the SQL and try again."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "A single DuckDB SELECT statement."},
            "purpose": {
                "type": "string",
                "description": "One short sentence: what this query checks or answers.",
            },
        },
        "required": ["sql", "purpose"],
        "additionalProperties": False,
    },
}

SUBMIT_ANSWER = {
    "name": "submit_answer",
    "description": (
        "Finish: submit the final SQL that answers the question, a short answer in plain "
        "language with the key numbers, and any assumptions. The SQL is re-executed and its "
        "result shown to the user. If the question cannot be answered from the warehouse, "
        "submit an empty sql string and say so in answer."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "Final DuckDB SELECT, or '' when the question is unanswerable.",
            },
            "answer": {"type": "string", "description": "The answer for the user."},
            "assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Interpretations you made (date windows, definitions); [] if none.",
            },
        },
        "required": ["sql", "answer", "assumptions"],
        "additionalProperties": False,
    },
}


def tool_definitions(schema_retrieval: bool = True) -> list[dict]:
    """Tool list in a fixed order (it is part of the cached prompt prefix)."""
    if schema_retrieval:
        return [SEARCH_SCHEMA, DESCRIBE_TABLE, RUN_SQL, SUBMIT_ANSWER]
    return [RUN_SQL, SUBMIT_ANSWER]


@dataclass
class ToolOutcome:
    content: str  # JSON text sent back as the tool_result
    is_error: bool = False
    kind: str = "ok"  # ok | sql_error | guard_rejection | timeout | bad_input
    data: dict = field(default_factory=dict)  # full result for the trace / final answer
    elapsed_ms: float = 0.0


def _dump(obj) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


class Toolbox:
    def __init__(self, index: SchemaIndex, db: ReadOnlyDB, result_rows: int = 50,
                 row_limit: int = 1000, error_chars: int = 1000):
        self.index = index
        self.db = db
        self.result_rows = result_rows
        self.error_chars = error_chars
        self.guard = SqlGuard(index.allowed_tables, db.catalog, db.statement_types, row_limit,
                              known_tables=db.relation_names())

    def call(self, name: str, args: dict) -> ToolOutcome:
        t0 = time.perf_counter()
        try:
            out = self._dispatch(name, args)
        except (KeyError, TypeError, ValueError) as e:
            out = ToolOutcome(_dump({"error": f"invalid input for {name}: {e}"}), True,
                              "bad_input")
        out.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        return out

    def _dispatch(self, name: str, args: dict) -> ToolOutcome:
        if not isinstance(args, dict):
            raise TypeError("tool input must be a JSON object")
        if name == "search_schema":
            hits = self.index.search(str(args["query"]), int(args.get("k", 5)))
            return ToolOutcome(_dump({"results": hits}),
                               data={"tables": [h["table"] for h in hits]})
        if name == "describe_table":
            doc = self.index.describe(str(args["table"]))
            if doc is None:
                names = ", ".join(d.fqn for d in self.index.docs)
                msg = f"unknown table {args['table']!r}. Available tables: {names}"
                return ToolOutcome(_dump({"error": msg}), True, "bad_input")
            return ToolOutcome(_dump(doc), data={"table": doc["table"]})
        if name == "run_sql":
            return self.run_sql(str(args["sql"]))
        raise ValueError(f"unknown tool {name!r}")

    # -- run_sql -------------------------------------------------------------------------
    def run_sql(self, sql: str, max_rows_to_model: int | None = None) -> ToolOutcome:
        n_model = self.result_rows if max_rows_to_model is None else max_rows_to_model
        try:
            checked = self.guard.check(sql)
        except GuardRejection as e:
            return ToolOutcome(_dump({"error": f"rejected by the SQL guard: {e}"}), True,
                               "guard_rejection", data={"reason": str(e)})
        except SqlSyntaxError as e:
            return ToolOutcome(_dump({"error": self._trim(str(e))}), True, "sql_error",
                               data={"error": str(e)})
        try:
            res = self.db.execute(checked.sql, max_rows=self.guard.row_limit)
        except QueryTimeout as e:
            return ToolOutcome(_dump({"error": f"{e}. Aggregate earlier or filter more."}),
                               True, "timeout", data={"error": str(e)})
        except duckdb.Error as e:
            msg = self._trim(str(e) + self._hint(str(e), checked.tables))
            return ToolOutcome(_dump({"error": msg}), True, "sql_error", data={"error": str(e)})
        rows = [[to_jsonable(v) for v in r] for r in res.rows]
        payload = {
            "columns": res.columns,
            "rows": rows[:n_model],
            "row_count": len(rows),
            "truncated": len(rows) > n_model or res.truncated,
        }
        if checked.limit_injected and len(rows) >= self.guard.row_limit:
            payload["note"] = f"LIMIT {self.guard.row_limit} was applied; more rows may exist"
        return ToolOutcome(_dump(payload), data={
            "sql": checked.sql, "columns": res.columns, "types": res.types, "rows": rows,
            "row_count": len(rows), "limit_injected": checked.limit_injected,
            "tables": checked.tables, "db_ms": res.elapsed_ms,
        })

    def _trim(self, msg: str) -> str:
        msg = msg.strip()
        return msg if len(msg) <= self.error_chars else msg[: self.error_chars - 1] + "…"

    def _hint(self, msg: str, tables: list[str]) -> str:
        """Add the available columns when DuckDB reports a missing column or table."""
        m = re.search(r'Referenced table "?(\w+)"? not found|Table with name (\w+) does not exist',
                      msg)
        if m:
            return "\nHint: available tables are " + ", ".join(d.name for d in self.index.docs)
        if re.search(r"Referenced column .* not found|does not have a column named", msg):
            hints = []
            for fq in tables:
                d = self.index.resolve(fq)
                if d is not None:
                    hints.append(f"{d.name}({', '.join(c.name for c in d.columns)})")
            if hints:
                return "\nHint: columns of the tables in this query - " + " | ".join(hints)
        return ""
