import json

from agent.config import SEED_DBT_TARGET
from eval.retrieval_probes import probe_scores


def test_crash_rate_by_firmware_ranks_fct_firmware_daily_first(index):
    hits = index.search("crash rate by firmware", 5)
    assert hits[0]["table"] == "marts.fct_firmware_daily"
    cols = [c["name"] for c in hits[0]["matching_columns"]]
    assert "crash_rate_per_100_device_days" in cols


def test_retrieval_probes(index):
    """Lexical retrieval is good, not perfect: the agent asks for k=5 and reads the
    descriptions, and the no-retrieval ablation in the eval measures what it costs."""
    s = probe_scores(index)
    assert s["top3"] >= 16, s["top1_misses"]
    assert s["top1"] >= 11, s["top1_misses"]


def test_only_marts_exposed_by_default(index):
    names = {d.name for d in index.docs}
    assert names == {"dim_date", "dim_device", "dim_event_code", "dim_firmware", "dim_region",
                     "fct_device_daily", "fct_device_event", "fct_device_minute",
                     "fct_firmware_daily", "fct_trip"}
    assert all(d.layer == "mart" for d in index.docs)


def test_staging_is_opt_in(db):
    from agent.schema_index import SchemaIndex

    idx = SchemaIndex.build(SEED_DBT_TARGET, None, expose_staging=True)
    assert any(d.name.startswith("stg_") for d in idx.docs)


def test_docs_have_descriptions_pk_joins_samples(index):
    d = index.describe("fct_device_daily")
    assert d["grain"] == ["device_id", "event_date"]
    cols = {c["name"]: c for c in d["columns"]}
    assert "registered = false" in cols["lte_loss_pct"]["description"]
    assert cols["lte_loss_pct"]["type"] == "DECIMAL(9,2)"
    assert "device_key -> marts.dim_device.device_key" in d["joins"]
    assert len(d["sample_rows"]) == 3
    assert d["date_coverage"]["event_date"][0] == "2026-01-01"
    assert index.describe("fct_firmware_daily")["grain"] == [
        "firmware_version", "hardware_model", "event_date"]
    assert index.describe("dim_device")["primary_key"] == ["device_key"]


def test_resolve_accepts_qualified_names(index):
    assert index.resolve("marts.fct_trip").name == "fct_trip"
    assert index.resolve('"fct_trip"').name == "fct_trip"
    assert index.resolve("nope") is None


def test_seed_schema_matches_dbt_catalog(db):
    """The seed must have the same columns, order and types as the platform's marts."""
    cat = json.loads((SEED_DBT_TARGET / "catalog.json").read_text())
    for node in cat["nodes"].values():
        if node["metadata"]["schema"] != "marts":
            continue
        name = node["metadata"]["name"]
        want = [(c["name"], c["type"]) for c in sorted(node["columns"].values(),
                                                       key=lambda c: c["index"])]
        got = db.execute(
            "select column_name, data_type from information_schema.columns "
            "where table_schema = 'marts' and table_name = ? order by ordinal_position",
            params=[name]).rows
        assert [(c, t) for c, t in got] == want, name


def test_all_schemas_text_is_deterministic(index):
    assert index.all_schemas_text() == index.all_schemas_text()
    assert "fct_firmware_daily" in index.all_schemas_text()
