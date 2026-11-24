"""Read-only, locked-down DuckDB access with a query timeout.

Layers 1, 2 and 4 of the guard live here (sql_guard.py is layer 3):

1. the file is opened with ``read_only=True``;
2. external access (files, URLs, extension installs/loads) is disabled at connect time and
   the configuration is then locked, so SQL cannot switch it back on;
4. every query runs in a worker thread with a timeout (``interrupt()`` on expiry) and a
   hard cap on fetched rows.

``read_only`` alone is not enough: on a read-only connection DuckDB still allows
``read_csv('/etc/passwd')``, ``query('...')``, ``CREATE TEMP TABLE``, ``PRAGMA`` and
``SET search_path``. Tests in tests/test_sql_guard.py show which layer stops what.
"""

from __future__ import annotations

import datetime as dt
import decimal
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import duckdb


class QueryTimeout(Exception):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    types: list[str]
    rows: list[tuple]
    truncated: bool  # more rows existed than max_rows
    elapsed_ms: float = 0.0
    meta: dict = field(default_factory=dict)


LOCKED_CONFIG = {
    "enable_external_access": False,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "allow_community_extensions": False,
    "allow_unsigned_extensions": False,
    "threads": 2,
    "memory_limit": "1GB",
}


class ReadOnlyDB:
    """One DuckDB instance per process; one cursor per query (thread-safe)."""

    def __init__(self, path: str | Path, schemas: tuple[str, ...] = ("marts",),
                 timeout_s: float = 10.0):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"warehouse not found: {self.path}. Build the telemetry platform, or run "
                "`make seed` to build the seed warehouse."
            )
        self.timeout_s = timeout_s
        self.schemas = schemas
        self._con = duckdb.connect(str(self.path), read_only=True, config=dict(LOCKED_CONFIG))
        self.catalog = self._con.execute("select current_database()").fetchone()[0]
        # DuckDB keeps one instance per file per process, so a second ReadOnlyDB on the
        # same file shares the (already locked) configuration.
        if not self._con.execute("select current_setting('lock_configuration')").fetchone()[0]:
            self._con.execute("SET lock_configuration = true")
        self._lock = threading.Lock()

    # -- helpers ---------------------------------------------------------------------
    def _cursor(self) -> duckdb.DuckDBPyConnection:
        with self._lock:
            cur = self._con.cursor()
        # search_path is a per-connection setting; it is set here (by us), never by the model
        cur.execute(f"SET search_path = '{','.join(self.schemas)}'")
        return cur

    def setting(self, name: str):
        cur = self._cursor()
        try:
            return cur.execute("select current_setting(?)", [name]).fetchone()[0]
        finally:
            cur.close()

    def relation_names(self) -> set[str]:
        """Names of every table/view in the file (all schemas), for typo detection."""
        cur = self._cursor()
        try:
            return {r[0] for r in cur.execute(
                "select table_name from information_schema.tables").fetchall()}
        finally:
            cur.close()

    def statement_types(self, sql: str) -> list[str]:
        """DuckDB's own parser: the type of each statement in ``sql``."""
        cur = self._cursor()
        try:
            return [s.type.name for s in cur.extract_statements(sql)]
        finally:
            cur.close()

    # -- execution -------------------------------------------------------------------
    def execute(self, sql: str, max_rows: int = 1000, timeout_s: float | None = None,
                params: list | None = None) -> QueryResult:
        """Run one statement with a timeout. Raises duckdb.Error or QueryTimeout."""
        timeout = self.timeout_s if timeout_s is None else timeout_s
        cur = self._cursor()
        box: dict = {}

        def work():
            try:
                rel = cur.execute(sql, params or [])
                desc = cur.description or []
                rows = rel.fetchmany(max_rows + 1)
                box["ok"] = (desc, rows)
            except BaseException as e:  # noqa: BLE001 - re-raised in the caller
                box["err"] = e

        t0 = time.perf_counter()
        th = threading.Thread(target=work, name=f"duckdb-{uuid.uuid4().hex[:6]}", daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            cur.interrupt()
            th.join(5)
            cur.close()
            raise QueryTimeout(f"query exceeded the {timeout:g} s timeout and was cancelled")
        cur.close()
        elapsed = (time.perf_counter() - t0) * 1000
        if "err" in box:
            raise box["err"]
        desc, rows = box["ok"]
        truncated = len(rows) > max_rows
        return QueryResult(
            columns=[d[0] for d in desc],
            types=[str(d[1]) for d in desc],
            rows=rows[:max_rows],
            truncated=truncated,
            elapsed_ms=round(elapsed, 1),
        )

    def close(self):
        self._con.close()


def to_jsonable(v):
    """Convert a DuckDB value into something json.dumps can write."""
    if v is None or isinstance(v, bool | int | str):
        return v
    if isinstance(v, float):
        return v if v == v else None  # NaN -> null
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, dt.datetime | dt.date | dt.time):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    if isinstance(v, list | tuple):
        return [to_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): to_jsonable(x) for k, x in v.items()}
    if isinstance(v, uuid.UUID):
        return str(v)
    return str(v)
