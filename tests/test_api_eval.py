"""FastAPI endpoint and the offline eval pipeline (mock client on the seed warehouse)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent import api
from agent.loop import Agent
from eval import run as eval_run
from eval.mock_client import GoldReplayClient, ScriptedClient, response, tool_use


def test_api_ask(settings, db, index):
    client = ScriptedClient([response(tool_use("submit_answer", {
        "sql": "SELECT count(DISTINCT device_id) AS n FROM marts.dim_device",
        "answer": "There are N devices.", "assumptions": []}))])
    api.STATE["agent"] = Agent(settings, client=client, db=db, index=index)
    try:
        with TestClient(api.app) as http:
            r = http.post("/ask", json={"question": "How many devices?"})
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "answered" and body["columns"] == ["n"]
            assert body["rows"][0][0] > 0 and body["trace"] and "latency_ms" in body
            assert set(body["usage"]) >= {"input_tokens", "cache_read_input_tokens"}
            assert http.get("/health").json()["warehouse"] == "seed"
            assert http.post("/ask", json={"question": ""}).status_code == 422
    finally:
        api.STATE.pop("agent", None)


def test_every_gold_sql_passes_guard_on_seed(toolbox, questions):
    for q in questions:
        if q["unanswerable"]:
            assert q["gold_sql"] == ""
            continue
        out = toolbox.run_sql(q["gold_sql"])
        assert not out.is_error, (q["id"], out.content)


def test_question_set_shape(questions):
    assert 40 <= len(questions) <= 60
    assert len({q["id"] for q in questions}) == len(questions)
    assert sum(q["unanswerable"] for q in questions) >= 5
    for b in ("easy", "medium", "hard"):
        assert sum(q["difficulty"] == b and not q["unanswerable"] for q in questions) >= 10
    for q in questions:
        assert set(q) >= {"id", "question", "gold_sql", "difficulty", "tags", "order_matters",
                          "verified_by"}


def test_mock_eval_pipeline_end_to_end(tmp_path, monkeypatch, questions):
    """CI's eval: the gold-replay client must score 100% and exercise self-correction."""
    import eval.report as report

    monkeypatch.setattr(report, "RESULTS", tmp_path)
    rc = eval_run.main(["--client", "mock", "--seed", "--configs", "main,no_retrieval",
                        "--workers", "2"])
    assert rc == 0
    rep = json.loads((tmp_path / "mock_seed.json").read_text())
    main = rep["configs"]["main"]
    assert rep["client"] == "mock-gold-replay"
    assert main["execution_accuracy_pct"] == 100.0
    assert main["abstention_accuracy_pct"] == 100.0
    assert main["guard_rejections"] == 0
    sc = main["self_correction"]
    assert sc["needed_retry_pct"] > 0 and sc["recovered_pct"] == 100.0
    assert rep["configs"]["no_retrieval"]["execution_accuracy_pct"] == 100.0
    assert not (tmp_path / "latest.json").exists()  # mock runs never become "latest"


def test_scoring_detects_wrong_answers(db, index, settings, questions):
    """A replay client that submits a wrong query is scored as wrong."""
    q = next(x for x in questions if x["id"] == "m01")
    wrong = dict(q, gold_sql=q["gold_sql"].replace("crash_count", "device_count"))
    agent = Agent(settings, client=GoldReplayClient([wrong], break_every=0), db=db, index=index)
    res = agent.ask(q["question"])
    rec = eval_run.score(q, res, eval_run.Executor(db, index), {})
    assert res.status == "answered" and rec["correct"] is False


@pytest.mark.parametrize("bad", ["", "SELECT 1"])
def test_unanswerable_scoring(db, index, settings, questions, bad):
    q = next(x for x in questions if x["unanswerable"])
    client = ScriptedClient([response(tool_use("submit_answer", {
        "sql": bad, "answer": "Here you go: 1", "assumptions": []}))])
    res = Agent(settings, client=client, db=db, index=index).ask(q["question"])
    rec = eval_run.score(q, res, eval_run.Executor(db, index), {})
    assert rec["correct"] is False
