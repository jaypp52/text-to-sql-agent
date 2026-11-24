"""Agent loop with the Claude client mocked (no API key, no network)."""

from __future__ import annotations

from types import SimpleNamespace

from agent.config import FALLBACK_BETA
from agent.loop import Agent
from eval.mock_client import ScriptedClient, response, text, tool_use, usage

GOOD = "SELECT firmware_version, round(100.0 * sum(crash_count) / sum(device_days), 2) AS r " \
       "FROM marts.fct_firmware_daily GROUP BY 1 ORDER BY 1"
BAD = GOOD.replace("crash_count", "crashes")


def make(settings, db, index, responses, **over):
    client = ScriptedClient(responses)
    return Agent(settings.with_(**over), client=client, db=db, index=index), client


def submit(sql, answer="ok", assumptions=None, id_=None):
    return tool_use("submit_answer", {"sql": sql, "answer": answer,
                                      "assumptions": assumptions or []}, id_)


def test_error_then_retry_then_success(settings, db, index):
    agent, client = make(settings, db, index, [
        response(text("Let me look."), tool_use("search_schema", {"query": "crash rate", "k": 5})),
        response(tool_use("describe_table", {"table": "fct_firmware_daily"})),
        response(tool_use("run_sql", {"sql": BAD, "purpose": "first try"})),
        response(tool_use("run_sql", {"sql": GOOD, "purpose": "fixed"})),
        response(submit(GOOD, "1.1.0 crashes more", ["whole window"])),
    ])
    r = agent.ask("Crash rate by firmware?")
    assert r.status == "answered", r.error
    assert r.sql == GOOD and r.columns == ["firmware_version", "r"] and r.row_count == 2
    assert r.sql_errors == 1 and r.retries == 1 and r.recovered and r.first_sql_ok is False
    assert r.turns == 5 and r.tool_calls == 5 and r.assumptions == ["whole window"]

    # the failed run_sql came back as is_error with the DuckDB message and a hint
    fourth = client.requests[3]["messages"]
    err = fourth[-1]["content"][0]
    assert err["type"] == "tool_result" and err["is_error"] is True
    assert "crashes" in err["content"] and "Hint" in err["content"]
    # every turn appended the full assistant content (text + tool_use blocks)
    assert fourth[1]["role"] == "assistant" and fourth[1]["content"][0].type == "text"


def test_request_parameters(settings, db, index):
    agent, client = make(settings, db, index, [response(submit("SELECT 1"))])
    agent.ask("q")
    req = client.requests[0]
    assert req["beta"] is True and req["betas"] == [FALLBACK_BETA]
    assert req["fallbacks"] == "default"
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"] == {"effort": "medium"}
    assert req["cache_control"] == {"type": "ephemeral"}
    assert req["system"][-1]["cache_control"] == {"type": "ephemeral"}
    for banned in ("temperature", "top_p", "top_k", "tool_choice", "thinking"):
        assert banned not in req
    names = [t["name"] for t in req["tools"]]
    assert names == ["search_schema", "describe_table", "run_sql", "submit_answer"]
    for t in req["tools"]:
        assert t["strict"] is True
        assert t["input_schema"]["additionalProperties"] is False
        assert set(t["input_schema"]["required"]) == set(t["input_schema"]["properties"])


def test_no_fallback_for_other_models(settings, db, index):
    agent, client = make(settings, db, index, [response(submit("SELECT 1"))],
                         model="claude-sonnet-5-5")
    agent.ask("q")
    assert client.requests[0]["beta"] is False and "fallbacks" not in client.requests[0]


def test_system_prompt_and_tools_identical_across_questions(settings, db, index):
    agent, client = make(settings, db, index, [response(submit("SELECT 1")),
                                               response(submit("SELECT 2"))])
    agent.ask("first")
    agent.ask("second")
    a, b = client.requests
    assert a["system"] == b["system"] and a["tools"] == b["tools"]


def test_parallel_tool_calls_answered_in_one_message(settings, db, index):
    agent, client = make(settings, db, index, [
        response(tool_use("describe_table", {"table": "fct_trip"}, "t1"),
                 tool_use("describe_table", {"table": "nope"}, "t2"),
                 tool_use("run_sql", {"sql": "SELECT count(*) FROM fct_trip", "purpose": "n"},
                          "t3")),
        response(submit("SELECT count(*) FROM fct_trip")),
    ])
    r = agent.ask("How many trips?")
    assert r.status == "answered"
    last = client.requests[1]["messages"][-1]
    assert last["role"] == "user" and [c["tool_use_id"] for c in last["content"]] == [
        "t1", "t2", "t3"]
    assert [c["is_error"] for c in last["content"]] == [False, True, False]


def test_sql_error_budget(settings, db, index):
    agent, _ = make(settings, db, index, [
        response(tool_use("run_sql", {"sql": BAD, "purpose": str(i)})) for i in range(3)
    ])
    r = agent.ask("q")
    assert r.status == "error" and r.error_kind == "sql_budget:sql_error"
    assert r.sql_errors == 3 and "crashes" in r.error


def test_submitted_sql_is_reexecuted(settings, db, index):
    agent, client = make(settings, db, index, [
        response(submit(BAD, id_="s1")),
        response(submit(GOOD)),
    ])
    r = agent.ask("q")
    assert r.status == "answered" and r.sql == GOOD and r.sql_errors == 1
    back = client.requests[1]["messages"][-1]["content"][0]
    assert back["tool_use_id"] == "s1" and back["is_error"] is True


def test_guard_rejection_is_returned_to_model(settings, db, index):
    agent, client = make(settings, db, index, [
        response(tool_use("run_sql", {"sql": "SELECT * FROM read_csv('/etc/passwd')",
                                      "purpose": "x"})),
        response(submit("SELECT 1")),
    ])
    r = agent.ask("q")
    assert r.guard_rejections == 1 and r.status == "answered"
    assert "rejected by the SQL guard" in client.requests[1]["messages"][-1]["content"][0][
        "content"]


def test_abstain_with_empty_sql(settings, db, index):
    agent, _ = make(settings, db, index, [
        response(submit("", "The warehouse does not contain driver names."))])
    r = agent.ask("driver name?")
    assert r.status == "abstained" and r.sql == "" and r.sql_attempts == []


def test_end_turn_nudges_once_then_fails(settings, db, index):
    agent, client = make(settings, db, index, [response(text("The answer is 5.")),
                                               response(text("Still 5."))])
    r = agent.ask("q")
    assert r.status == "error" and r.error_kind == "no_submit"
    assert "submit_answer" in client.requests[1]["messages"][-1]["content"]


def test_end_turn_then_submit_after_nudge(settings, db, index):
    agent, _ = make(settings, db, index, [response(text("5")), response(submit("SELECT 5"))])
    r = agent.ask("q")
    assert r.status == "answered" and r.rows == [[5]]


def test_refusal_and_max_tokens(settings, db, index):
    refusal = response(stop_reason="refusal",
                       stop_details=SimpleNamespace(category="cyber", explanation=None))
    agent, _ = make(settings, db, index, [refusal])
    r = agent.ask("q")
    assert r.status == "error" and r.error_kind == "refusal" and "cyber" in r.error
    # a truncated tool_use must not run
    cut = response(tool_use("run_sql", {"sql": "SELECT", "purpose": ""}), stop_reason="max_tokens")
    agent, _ = make(settings, db, index, [cut])
    r = agent.ask("q")
    assert r.error_kind == "max_tokens" and r.sql_attempts == []


def test_max_turns(settings, db, index):
    loop = [response(tool_use("search_schema", {"query": "x", "k": 1})) for _ in range(3)]
    agent, _ = make(settings, db, index, loop, max_turns=3)
    r = agent.ask("q")
    assert r.status == "error" and r.error_kind == "max_turns" and r.turns == 3


def test_usage_and_cost(settings, db, index):
    agent, _ = make(settings, db, index, [
        response(tool_use("search_schema", {"query": "x", "k": 1}),
                 u=usage(inp=1000, out=200, cache_write=3000)),
        response(submit("SELECT 1"), u=usage(inp=500, out=100, cache_read=3000)),
    ])
    r = agent.ask("q")
    assert r.usage["input_tokens"] == 1500 and r.usage["cache_read_input_tokens"] == 3000
    # opus 5.5: 1500*4 + 300*20 + 3000*5 + 3000*0.2 per 1M
    assert abs(r.cost_usd - (1500 * 4 + 300 * 20 + 3000 * 5 + 3000 * 0.2) / 1e6) < 1e-12


def test_fallback_blocks_echoed_correctly(settings, db, index):
    fb = SimpleNamespace(type="fallback")
    thinking = SimpleNamespace(type="thinking", thinking="", signature="x")
    first = response(thinking, tool_use("search_schema", {"query": "x", "k": 1}, "old"), fb,
                     tool_use("search_schema", {"query": "y", "k": 1}, "new"))
    first.usage.iterations = [SimpleNamespace(type="message", input_tokens=1, output_tokens=1),
                              SimpleNamespace(type="fallback_message", input_tokens=1,
                                              output_tokens=1)]
    agent, client = make(settings, db, index, [first, response(submit("SELECT 1"))])
    r = agent.ask("q")
    assert r.fallback_used
    echoed = client.requests[1]["messages"][1]["content"]
    assert [b.type for b in echoed] == ["fallback", "tool_use"] and echoed[1].id == "new"


def test_no_retrieval_ablation_pastes_schemas(settings, db, index):
    agent, client = make(settings, db, index, [response(submit("SELECT 1"))],
                         schema_retrieval=False)
    agent.ask("q")
    req = client.requests[0]
    assert [t["name"] for t in req["tools"]] == ["run_sql", "submit_answer"]
    assert "fct_firmware_daily" in req["system"][0]["text"]
    assert "<warehouse_schema>" in req["system"][0]["text"]
