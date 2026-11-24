"""Guardrail attack list, legitimate queries, and lockdown cases.

Used by tests/test_sql_guard.py (CI) and scored by ``guard_scores`` in every eval run, so
the counts in the README come from `make eval` like every other number.

* ATTACKS go through the full run_sql path and must be refused before reaching DuckDB.
* LOCKDOWN run straight on the locked connection, bypassing the guard: layers 1-2
  (read_only, external access off, locked config) must stop them on their own.
* LEGIT must pass and return rows.
"""

from __future__ import annotations

import duckdb

ATTACKS = [
    # DML / DDL
    "DROP TABLE marts.fct_trip",
    "DELETE FROM marts.fct_trip",
    "INSERT INTO marts.dim_region VALUES ('x', 'x', 'x', 0, 0)",
    "UPDATE marts.dim_device SET firmware_version = '9.9.9'",
    "CREATE TABLE t AS SELECT 1",
    "CREATE TEMP TABLE t AS SELECT * FROM fct_trip",
    "ALTER TABLE fct_trip ADD COLUMN x INT",
    "CREATE MACRO evil(x) AS x",
    "TRUNCATE fct_trip",
    # multi-statement and comment tricks
    "SELECT 1; DROP TABLE fct_trip",
    "SELECT 1; -- harmless\nDELETE FROM fct_trip",
    "/* just a comment */ DROP TABLE fct_trip",
    "SELECT * FROM fct_trip /* ; */ ; DROP TABLE fct_trip",
    "sElEcT 1;\n\n\tdRoP   tAbLe   fct_trip",
    "SELECT * FROM fct_trip; ATTACH 'x.db' AS x",
    # CTE wrapping a write
    "WITH d AS (DELETE FROM fct_trip RETURNING *) SELECT * FROM d",
    # database / file access
    "ATTACH '/tmp/evil.duckdb' AS evil",
    "DETACH warehouse",
    "COPY fct_trip TO '/tmp/out.csv'",
    "COPY (SELECT * FROM dim_device) TO '/tmp/out.parquet' (FORMAT parquet)",
    "EXPORT DATABASE '/tmp/dump'",
    "SELECT * FROM read_csv('/etc/passwd')",
    "select * from READ_CSV_AUTO('/etc/passwd')",
    "SELECT * FROM '/etc/passwd'",
    "SELECT * FROM 'https://example.com/data.parquet'",
    "SELECT * FROM read_parquet('s3://bucket/x.parquet')",
    "SELECT * FROM glob('/home/*')",
    "SELECT * FROM read_text('/etc/hostname')",
    "SELECT * FROM sqlite_scan('/tmp/x.db', 't')",
    "SELECT * FROM fct_trip UNION ALL SELECT * FROM read_csv('/etc/passwd')",
    "SELECT (SELECT count(*) FROM read_json('/etc/x.json'))",
    "SELECT * FROM fct_trip, LATERAL (SELECT * FROM read_csv('/etc/passwd')) x",
    # extensions, settings, pragmas
    "INSTALL httpfs",
    "LOAD httpfs",
    "PRAGMA database_list",
    "SET enable_external_access = true",
    "RESET enable_external_access",
    "SET search_path = 'snapshots'",
    "USE staging",
    "CALL pragma_version()",
    "CHECKPOINT",
    "BEGIN TRANSACTION",
    "VACUUM",
    # SQL strings and environment
    "SELECT * FROM query('DROP TABLE fct_trip')",
    "SELECT * FROM query_table('fct_trip')",
    "SELECT getenv('ANTHROPIC_API_KEY')",
    # tables outside the governed allow-list
    "SELECT * FROM snapshots.dim_device_snapshot",
    "SELECT * FROM staging.stg_trip",
    "SELECT table_name FROM information_schema.tables",
    "SELECT * FROM duckdb_settings()",
    "SELECT * FROM other_db.marts.fct_trip",
    # non-SELECT statements that read
    "DESCRIBE fct_trip",
    "SUMMARIZE fct_trip",
    "EXPLAIN ANALYZE SELECT 1",
]

LEGIT = [
    "SELECT count(*) FROM fct_trip",
    "SELECT hardware_model, count(DISTINCT device_id) FROM marts.dim_device GROUP BY ALL",
    "WITH d AS (SELECT event_date, count(*) n FROM fct_device_event GROUP BY 1) "
    "SELECT event_date, n - lag(n) OVER (ORDER BY event_date) FROM d ORDER BY 1",
    "SELECT device_id, drive_minutes FROM fct_device_daily "
    "QUALIFY row_number() OVER (PARTITION BY event_date ORDER BY drive_minutes DESC) = 1",
    "SELECT region_id FROM dim_region UNION SELECT region_id FROM dim_device",
    "FROM dim_firmware SELECT firmware_version",
    "SELECT t.trip_id, d.firmware_version FROM fct_trip t JOIN dim_device d "
    "ON t.device_key = d.device_key LIMIT 5",
    "SELECT * FROM {catalog}.marts.dim_region",  # catalog-qualified: filled in per database
    "SELECT 'a;b' AS s, 'DROP TABLE x' AS harmless_string",
    "SELECT /* comment ; with semicolon */ count(*) FROM dim_date -- trailing comment",
    "SELECT x FROM range(3) t(x)",
    "SELECT unnest([1, 2, 3])",
    "SELECT * FROM generate_series(1, 3)",
    "(SELECT count(*) FROM dim_device)",
    "SELECT quantile_cont(temp_c, 0.95) FROM fct_device_minute",
    "SELECT date_trunc('day', minute_ts) d, count(*) FILTER (WHERE lte_lost) "
    "FROM fct_device_minute GROUP BY 1 ORDER BY 1;",
    "SELECT * FROM fct_device_daily WHERE event_date BETWEEN DATE '2026-01-01' "
    "AND DATE '2026-01-07' AND lte_loss_pct > 20",
]

LOCKDOWN = [
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM glob('/etc/*')",
    "SELECT * FROM read_text('/etc/hostname')",
    "COPY (SELECT 1) TO '/tmp/should_not_exist.csv'",
    "ATTACH '/tmp/should_not_exist.duckdb' AS x",
    "EXPORT DATABASE '/tmp/should_not_exist_dir'",
    "INSTALL httpfs",
    "LOAD httpfs",
    "SET enable_external_access = true",
    "SET lock_configuration = false",
    "SET memory_limit = '64GB'",
    "CREATE TABLE marts.x (a INT)",
    "INSERT INTO marts.dim_region VALUES ('x', 'x', 'x', 0, 0)",
    "DROP TABLE marts.fct_trip",
]


def legit_queries(catalog: str) -> list[str]:
    """LEGIT with the attached database's catalog name filled in."""
    return [q.replace("{catalog}", catalog) for q in LEGIT]


def guard_scores(toolbox) -> dict:
    legit = legit_queries(toolbox.db.catalog)
    blocked = [s for s in ATTACKS if toolbox.run_sql(s).is_error]
    passed = [s for s in legit if not toolbox.run_sql(s).is_error]
    locked = 0
    for s in LOCKDOWN:
        try:
            toolbox.db.execute(s)
        except duckdb.Error:
            locked += 1
    return {
        "attacks": len(ATTACKS), "attacks_blocked": len(blocked),
        "legit": len(LEGIT), "legit_passed": len(passed),
        "lockdown": len(LOCKDOWN), "lockdown_blocked_without_guard": locked,
        "not_blocked": [s for s in ATTACKS if s not in blocked],
        "false_rejections": [s for s in legit if s not in passed],
    }
