"""Checks against the telemetry platform's real warehouse.duckdb (skipped when absent,
e.g. in CI, which uses the seed)."""

from __future__ import annotations

import pytest

from agent.config import DEFAULT_DBT_TARGET, DEFAULT_WAREHOUSE
from agent.db import ReadOnlyDB
from agent.schema_index import SchemaIndex
from agent.tools import Toolbox
from eval.retrieval_probes import probe_scores

pytestmark = [
    pytest.mark.warehouse,
    pytest.mark.skipif(not DEFAULT_WAREHOUSE.exists(), reason="platform warehouse not built"),
]


@pytest.fixture(scope="module")
def platform():
    db = ReadOnlyDB(DEFAULT_WAREHOUSE)
    idx = SchemaIndex.build(DEFAULT_DBT_TARGET, db)
    yield db, idx, Toolbox(idx, db)
    db.close()


def test_connects_read_only_and_locked(platform):
    db, _, _ = platform
    assert db.setting("enable_external_access") is False
    with pytest.raises(Exception):  # noqa: B017 - any DuckDB error
        db.execute("CREATE TABLE marts.x (a INT)")


def test_index_and_search(platform):
    _, idx, _ = platform
    assert idx.search("crash rate by firmware", 5)[0]["table"] == "marts.fct_firmware_daily"
    assert probe_scores(idx)["top3"] >= 16
    assert idx.describe("dim_device")["row_count"] >= 500


def test_attack_list_on_platform(platform):
    from eval.attacks import guard_scores

    _, _, tb = platform
    s = guard_scores(tb)
    assert s["attacks_blocked"] == s["attacks"] and s["false_rejections"] == []


def test_every_gold_sql_runs_on_platform(platform, questions):
    _, _, tb = platform
    for q in questions:
        if q["gold_sql"]:
            out = tb.run_sql(q["gold_sql"])
            assert not out.is_error, (q["id"], out.content)
            assert out.data["row_count"] >= 1, q["id"]  # every gold answer is non-empty
