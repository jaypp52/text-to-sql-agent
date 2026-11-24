"""The real ``anthropic`` SDK against a local fake Messages endpoint (no key, no network).

Checks what actually goes over the wire: the beta header for the server-side fallback,
``fallbacks: "default"``, effort, both cache breakpoints, strict tools, no sampling
parameters or forced tool_choice; and that SDK response objects (pydantic models) flow
through the loop and are echoed back in the next request.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import pytest

from agent.loop import Agent


def msg(content, stop="tool_use", n=1):
    return {
        "id": f"msg_{n}", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 50, "output_tokens": 20, "cache_read_input_tokens": 900,
                  "cache_creation_input_tokens": 0},
    }


SCRIPT = [
    msg([{"type": "text", "text": "Counting devices."},
         {"type": "tool_use", "id": "toolu_a", "name": "run_sql",
          "input": {"sql": "SELECT count(DISTINCT device_id) FROM dim_devices", "purpose": "n"}}]),
    msg([{"type": "tool_use", "id": "toolu_b", "name": "run_sql",
          "input": {"sql": "SELECT count(DISTINCT device_id) FROM dim_device", "purpose": "n"}}],
        n=2),
    msg([{"type": "tool_use", "id": "toolu_c", "name": "submit_answer",
          "input": {"sql": "SELECT count(DISTINCT device_id) AS n FROM dim_device",
                    "answer": "60 devices.", "assumptions": []}}], n=3),
]


@pytest.fixture()
def fake_api():
    seen: list[dict] = []
    script = list(SCRIPT)

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
            out = json.dumps(script.pop(0)).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()


def test_wire_format_and_roundtrip(fake_api, settings, db, index):
    url, seen = fake_api
    client = anthropic.Anthropic(api_key="test-key", base_url=url, max_retries=0)
    agent = Agent(settings, client=client, db=db, index=index)
    r = agent.ask("How many devices are there?")
    assert r.status == "answered", r.error
    assert r.rows == [[60]] and r.sql_errors == 1 and r.recovered

    first = seen[0]
    assert first["path"].startswith("/v1/messages")
    assert "server-side-fallback-2026-07-01" in first["headers"].get("anthropic-beta", "")
    b = first["body"]
    assert b["model"] == "claude-opus-5-5" and b["fallbacks"] == "default"
    assert b["output_config"] == {"effort": "medium"}
    assert b["cache_control"] == {"type": "ephemeral"}
    assert b["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert all(t["strict"] is True for t in b["tools"])
    for banned in ("temperature", "top_p", "top_k", "tool_choice", "thinking"):
        assert banned not in b
    assert b["messages"] == [{"role": "user", "content": "How many devices are there?"}]

    # second request echoes the full assistant content and the is_error tool_result
    m = seen[1]["body"]["messages"]
    assert [x["role"] for x in m] == ["user", "assistant", "user"]
    assert [c["type"] for c in m[1]["content"]] == ["text", "tool_use"]
    res = m[2]["content"][0]
    assert res["tool_use_id"] == "toolu_a" and res["is_error"] is True
    assert "dim_devices" in res["content"]
    assert r.usage["cache_read_input_tokens"] == 2700 and r.usage["api_calls"] == 3
