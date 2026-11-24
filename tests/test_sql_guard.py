"""Guardrails: the attack list must be refused, legitimate queries must pass, and the
locked connection must stop file/extension/config attacks even without the guard.
The lists live in eval/attacks.py. No API key needed; runs on the seed warehouse.
"""

from __future__ import annotations

import json
import shutil
import time

import duckdb
import pytest

from agent.db import QueryTimeout, ReadOnlyDB
from agent.sql_guard import GuardRejection, SqlSyntaxError, UnknownTable
from eval.attacks import ATTACKS, LEGIT, LOCKDOWN, guard_scores


@pytest.mark.parametrize("sql", ATTACKS)
def test_attack_rejected_by_guard(toolbox, sql):
    with pytest.raises((GuardRejection, SqlSyntaxError)) as exc:
        toolbox.guard.check(sql)
    assert not isinstance(exc.value, UnknownTable), "attack misread as a typo"
    out = toolbox.run_sql(sql)
    assert out.is_error
    assert json.loads(out.content)["error"]


def test_attack_list_is_big_enough(toolbox):
    assert len(ATTACKS) >= 25
    assert len(LEGIT) >= 10
    s = guard_scores(toolbox)
    assert s["attacks_blocked"] == s["attacks"] and s["legit_passed"] == s["legit"]
    assert s["lockdown_blocked_without_guard"] == s["lockdown"]


@pytest.mark.parametrize("sql", LEGIT)
def test_legit_query_allowed(toolbox, sql):
    out = toolbox.run_sql(sql)
    assert not out.is_error, out.content
    assert out.data["row_count"] >= 1


# -- layers 1 and 2, without the guard ----------------------------------------------------


@pytest.mark.parametrize("sql", LOCKDOWN)
def test_locked_connection_blocks_without_guard(db, sql):
    with pytest.raises(duckdb.Error):
        db.execute(sql)


def test_config_is_locked(db):
    assert db.setting("enable_external_access") is False
    assert db.setting("lock_configuration") is True


def test_read_only_alone_is_not_enough(seed_path, tmp_path):
    """Why the guard is layered: a plain read_only connection still reads files and runs
    SQL strings. (This is the connection we do NOT use.)"""
    secret = tmp_path / "secret.csv"
    secret.write_text("k,v\napi_key,sk-not-real\n")
    copy = tmp_path / "copy.duckdb"  # DuckDB shares one instance per file and process
    shutil.copy(seed_path, copy)
    con = duckdb.connect(str(copy), read_only=True)
    try:
        assert con.execute(f"SELECT v FROM read_csv('{secret}')").fetchone()[0] == "sk-not-real"
        assert con.execute("SELECT * FROM query('SELECT 42')").fetchone()[0] == 42
        con.execute("CREATE TEMP TABLE scratch AS SELECT 1")  # writes are not all blocked
    finally:
        con.close()


def test_limit_injected_when_absent(toolbox):
    checked = toolbox.guard.check("SELECT * FROM fct_device_minute -- no limit")
    assert checked.limit_injected and checked.sql.endswith("LIMIT 1000")
    out = toolbox.run_sql("SELECT * FROM fct_device_minute;")
    assert out.data["row_count"] == 1000 and out.data["limit_injected"]
    payload = json.loads(out.content)
    assert len(payload["rows"]) == 50 and payload["truncated"]


def test_existing_limit_kept(toolbox):
    checked = toolbox.guard.check("SELECT * FROM fct_trip ORDER BY distance_km DESC LIMIT 3")
    assert not checked.limit_injected
    assert toolbox.run_sql(checked.sql).data["row_count"] == 3


def test_union_gets_outer_limit(toolbox):
    checked = toolbox.guard.check("SELECT 1 UNION ALL SELECT 2 ORDER BY 1")
    assert checked.limit_injected
    assert toolbox.run_sql("SELECT 1 UNION ALL SELECT 2 ORDER BY 1").data["rows"] == [[1], [2]]


def test_typo_is_an_sql_error_not_a_rejection(toolbox):
    out = toolbox.run_sql("SELECT * FROM fct_trips")
    assert out.is_error and out.kind == "sql_error"
    assert "fct_trip" in json.loads(out.content)["error"]


def test_missing_column_error_has_hint(toolbox):
    out = toolbox.run_sql("SELECT device_idd FROM fct_trip")
    assert out.kind == "sql_error"
    msg = json.loads(out.content)["error"]
    assert "device_idd" in msg and "Hint" in msg and "trip_id" in msg
    assert len(msg) <= 1000


def test_rejection_reason_is_returned(toolbox):
    out = toolbox.run_sql("SELECT * FROM read_csv('/etc/passwd')")
    assert out.kind == "guard_rejection"
    assert "read_csv" in json.loads(out.content)["error"]


def test_timeout_interrupts_query(seed_path):
    slow = ReadOnlyDB(seed_path, timeout_s=0.5)
    t0 = time.perf_counter()
    with pytest.raises(QueryTimeout):
        slow.execute("SELECT sum(a.range * b.range) FROM range(200000) a, range(200000) b")
    assert time.perf_counter() - t0 < 5
    assert slow.execute("SELECT 1").rows == [(1,)]  # still usable afterwards
    slow.close()
