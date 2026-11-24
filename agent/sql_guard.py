"""SQL guard: layer 3 of the read-only defence (see db.py for layers 1, 2 and 4).

Every SQL string from the model passes through ``SqlGuard.check`` before it reaches
DuckDB. The checks, in order:

a. DuckDB's own parser (``extract_statements``): exactly one statement, of type SELECT.
   A syntax error here is returned as a normal SQL error so the model can fix it.
b. sqlglot AST (dialect ``duckdb``):
   - the root is a SELECT / set operation (UNION, INTERSECT, EXCEPT), optionally inside
     parentheses or a WITH clause;
   - no DML/DDL/command node anywhere in the tree (catches a DELETE hidden in a CTE);
   - no table function except ``range``/``generate_series``/``unnest``, and no
     file/network/SQL-string function anywhere (``read_csv``, ``glob``, ``query``, ...);
   - every referenced table is an exposed model (or a CTE defined in the query); quoted
     file paths and URLs used as table names are rejected by name.
c. ``LIMIT <row_limit>`` is appended to the outer query when it has none.

Rejections carry a human-readable reason that is sent back to the model.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import duckdb
import sqlglot
from sqlglot import exp

log = logging.getLogger(__name__)
logging.getLogger("sqlglot").setLevel(logging.ERROR)

MAX_SQL_CHARS = 20_000

FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name)
    for name in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterColumn",
        "TruncateTable", "Command", "Copy", "Set", "SetItem", "Pragma", "Use", "Attach",
        "Detach", "Install", "Load", "Transaction", "Commit", "Rollback", "LoadData",
        "Export", "Describe", "Summarize", "Pivot", "Analyze", "Grant", "Revoke", "Kill",
        "Into",
    )
    if hasattr(exp, name)
)

# Pivot is a statement in DuckDB but sqlglot also uses exp.Pivot for the PIVOT clause in
# a FROM; both are rejected (the agent can use FILTER / CASE aggregates instead).

ALLOWED_TABLE_FUNCTIONS = {"range", "generate_series", "unnest"}

# Functions that read files/URLs, run SQL strings, or expose the environment.
FORBIDDEN_FUNCTION_NAMES = {
    "read_csv", "read_csv_auto", "read_parquet", "parquet_scan", "read_json",
    "read_json_auto", "read_json_objects", "read_json_objects_auto", "read_ndjson",
    "read_ndjson_auto", "read_ndjson_objects", "read_text", "read_blob", "read_xlsx",
    "glob", "sniff_csv", "parquet_metadata", "parquet_schema", "parquet_file_metadata",
    "parquet_kv_metadata", "iceberg_scan", "iceberg_metadata", "delta_scan", "sqlite_scan",
    "sqlite_attach", "postgres_scan", "postgres_attach", "mysql_scan", "st_read",
    "query", "query_table", "getenv", "load_extension", "install_extension",
    "duckdb_secrets", "which_secret", "pragma_storage_info", "checkpoint",
    "force_checkpoint",
}
FORBIDDEN_FUNCTION_CLASSES = tuple(
    getattr(exp, n) for n in ("ReadCSV", "ReadParquet", "ReadJSON") if hasattr(exp, n)
)

PATHLIKE = re.compile(r"[/\\:*?]|\.(csv|tsv|parquet|json|ndjson|txt|db|duckdb|sqlite|xlsx)$",
                      re.IGNORECASE)


class GuardRejection(Exception):
    """The SQL was refused by the guard (not a database error)."""


class SqlSyntaxError(Exception):
    """DuckDB's parser could not parse the SQL (returned to the model as an SQL error)."""


class UnknownTable(SqlSyntaxError):
    """The query names a table that does not exist anywhere (a typo, not an attack)."""


@dataclass
class GuardResult:
    sql: str  # the SQL that will run (LIMIT possibly appended)
    tables: list[str] = field(default_factory=list)
    limit_injected: bool = False


def _func_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    try:
        return (node.sql_name() or "").lower()
    except Exception:  # noqa: BLE001
        return type(node).__name__.lower()


class SqlGuard:
    def __init__(self, allowed_tables: dict[str, set[str]], catalog: str,
                 statement_types, row_limit: int = 1000, known_tables: set[str] | None = None):
        """
        allowed_tables: schema -> table names the model may reference, e.g.
            {"marts": {"dim_device", "fct_trip", ...}}
        catalog: the database (catalog) name of the attached warehouse file.
        statement_types: callable(sql) -> list of DuckDB statement type names.
        known_tables: every relation name in the database; an unqualified name outside it
            is reported as an unknown table (an ordinary SQL error the model can fix)
            rather than as a guard rejection.
        """
        self.known = {t.lower() for t in known_tables} if known_tables is not None else None
        self.allowed = {s.lower(): {t.lower() for t in ts} for s, ts in allowed_tables.items()}
        self.all_tables = set().union(*self.allowed.values()) if self.allowed else set()
        self.catalog = catalog.lower()
        self.statement_types = statement_types
        self.row_limit = row_limit

    def check(self, sql: str, row_limit: int | None = None) -> GuardResult:
        limit = self.row_limit if row_limit is None else row_limit
        if not isinstance(sql, str) or not sql.strip():
            raise GuardRejection("empty SQL")
        if len(sql) > MAX_SQL_CHARS:
            raise GuardRejection(f"SQL longer than {MAX_SQL_CHARS} characters")
        sql = sql.strip()

        # (a) DuckDB's own parser: one statement, SELECT
        try:
            types = self.statement_types(sql)
        except duckdb.Error as e:
            raise SqlSyntaxError(str(e)) from e
        if len(types) != 1:
            raise GuardRejection(
                f"exactly one statement is allowed, got {len(types)} "
                f"({', '.join(types) or 'none'}); send a single SELECT"
            )
        if types[0] != "SELECT":
            raise GuardRejection(f"only SELECT queries are allowed, got a {types[0]} statement")

        # (b) sqlglot AST checks
        body = sql.rstrip().rstrip(";").rstrip()
        try:
            trees = [t for t in sqlglot.parse(body, read="duckdb") if t is not None]
        except sqlglot.errors.SqlglotError as e:
            raise GuardRejection(
                f"the guard could not analyse this SQL ({str(e).splitlines()[0][:200]}); "
                "rewrite it with standard SELECT syntax"
            ) from e
        if len(trees) != 1:
            raise GuardRejection(f"exactly one statement is allowed, got {len(trees)}")
        root = trees[0]
        inner = root
        while isinstance(inner, exp.Subquery):
            inner = inner.this
        if not isinstance(inner, exp.Select | exp.SetOperation):
            raise GuardRejection(
                f"only SELECT queries are allowed, got {type(inner).__name__.upper()}"
            )

        for node in root.walk():
            if isinstance(node, FORBIDDEN_NODES):
                raise GuardRejection(
                    f"{type(node).__name__.upper()} is not allowed (read-only SELECT queries only)"
                )
            if isinstance(node, FORBIDDEN_FUNCTION_CLASSES):
                raise GuardRejection(
                    f"function {_func_name(node)}() is not allowed: it reads files or URLs"
                )
            if isinstance(node, exp.Func):
                name = _func_name(node)
                if name in FORBIDDEN_FUNCTION_NAMES or name.startswith(("read_", "pragma_")):
                    raise GuardRejection(
                        f"function {name}() is not allowed: it reads files/URLs, runs SQL "
                        "strings or exposes the environment"
                    )

        cte_names = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
        tables: list[str] = []
        for t in root.find_all(exp.Table):
            target = t.this
            if isinstance(target, exp.Func):
                fname = _func_name(target)
                if isinstance(target, exp.GenerateSeries) or fname in ALLOWED_TABLE_FUNCTIONS:
                    continue
                raise GuardRejection(
                    f"table function {fname}() is not allowed; query the warehouse tables only"
                )
            name, db, cat = t.name.lower(), t.db.lower(), t.catalog.lower()
            if PATHLIKE.search(t.name):
                raise GuardRejection(
                    f"'{t.name}' looks like a file path or URL; only warehouse tables "
                    "can be queried"
                )
            if not db and not cat and name in cte_names:
                continue
            if cat and cat != self.catalog:
                raise GuardRejection(f"database '{t.catalog}' is not allowed")
            if db and db not in self.allowed:
                raise GuardRejection(
                    f"schema '{t.db}' is not exposed; allowed tables: {self._allowed_list()}"
                )
            ok = name in self.allowed[db] if db else name in self.all_tables
            label = f"{t.db}.{t.name}" if t.db else t.name
            if not ok and not db and self.known is not None and name not in self.known:
                raise UnknownTable(
                    f"Catalog Error: Table with name {t.name} does not exist! "
                    f"Available tables: {self._allowed_list()}"
                )
            if not ok:
                raise GuardRejection(
                    f"table '{label}' is not in the allow-list; "
                    f"allowed tables: {self._allowed_list()}"
                )
            tables.append(f"{db or self._schema_of(name)}.{name}")

        # (c) LIMIT on the outer query
        injected = False
        if inner.args.get("limit") is None and root.args.get("limit") is None:
            body = f"{body}\nLIMIT {int(limit)}"
            injected = True
        return GuardResult(sql=body, tables=sorted(set(tables)), limit_injected=injected)

    def _schema_of(self, table: str) -> str:
        for s, ts in self.allowed.items():
            if table in ts:
                return s
        return "?"

    def _allowed_list(self) -> str:
        return ", ".join(sorted(self.all_tables))
