"""Offline stand-ins for ``anthropic.Anthropic`` used by tests and by CI's eval run.

Neither talks to the network. They return objects with the same attributes the agent
loop reads from real SDK responses (``stop_reason``, ``content`` blocks with ``type``,
``id``, ``name``, ``input``/``text``, ``usage``), and record every request so tests can
assert on the parameters the loop sends.

* ``ScriptedClient`` replays a fixed list of responses (unit tests).
* ``GoldReplayClient`` plays a plausible agent for every labelled question: search the
  schema, describe a table, sometimes send a broken query first (to exercise
  self-correction), then run and submit the gold SQL. Its eval numbers test the
  *pipeline* (loop, guard, compare, report), not a model: they are written to
  eval/results/mock_*.json and never to the README.
"""

from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace


def usage(inp=0, out=0, cache_read=0, cache_write=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out,
                           cache_read_input_tokens=cache_read,
                           cache_creation_input_tokens=cache_write, iterations=None)


def tool_use(name: str, inp: dict, id_: str | None = None):
    id_ = id_ or "toolu_" + hashlib.md5(f"{name}{inp}".encode()).hexdigest()[:12]  # noqa: S324
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


def text(t: str):
    return SimpleNamespace(type="text", text=t)


def response(*blocks, stop_reason: str | None = None, u=None, model="mock", stop_details=None):
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, usage=u or usage(),
                           model=model, stop_details=stop_details)


class _Messages:
    def __init__(self, owner, beta: bool):
        self.owner, self.beta = owner, beta

    def create(self, **params):
        # snapshot the message list: the loop keeps appending to the same list
        self.owner.requests.append({"beta": self.beta, **params,
                                    "messages": list(params["messages"])})
        return self.owner.next(params)


class _Base:
    def __init__(self):
        self.requests: list[dict] = []
        self.messages = _Messages(self, beta=False)
        self.beta = SimpleNamespace(messages=_Messages(self, beta=True))

    def next(self, params):  # pragma: no cover - overridden
        raise NotImplementedError


class ScriptedClient(_Base):
    def __init__(self, responses: list):
        super().__init__()
        self.responses = list(responses)

    def next(self, params):
        if not self.responses:
            raise AssertionError("ScriptedClient ran out of responses")
        r = self.responses.pop(0)
        return r(params) if callable(r) else r


FROM_TABLE = re.compile(r"\bmarts\.(\w+)", re.IGNORECASE)


class GoldReplayClient(_Base):
    """Deterministic fake agent driven by the labelled set (see module docstring)."""

    def __init__(self, questions: list[dict], break_every: int = 4):
        super().__init__()
        self.by_question = {q["question"]: q for q in questions}
        self.break_every = break_every

    def next(self, params):
        msgs = params["messages"]
        q = self.by_question[msgs[0]["content"]]
        tools = {t["name"] for t in params["tools"]}
        step = sum(1 for m in msgs if m["role"] == "assistant")
        plan = self._plan(q, "search_schema" in tools)
        u = usage()
        return response(*plan[min(step, len(plan) - 1)], u=u, model=params["model"])

    def _plan(self, q: dict, retrieval: bool) -> list[list]:
        steps: list[list] = []
        if retrieval:
            steps.append([text("Looking up the schema."),
                          tool_use("search_schema", {"query": q["question"], "k": 5})])
        if q["unanswerable"]:
            steps.append([tool_use("submit_answer", {
                "sql": "", "answer": "This cannot be answered: the warehouse does not contain "
                                    "that data.", "assumptions": []})])
            return steps
        tables = FROM_TABLE.findall(q["gold_sql"])
        if retrieval and tables:
            steps.append([tool_use("describe_table", {"table": tables[0]})])
        n = int(hashlib.md5(q["id"].encode()).hexdigest(), 16)  # noqa: S324
        if self.break_every and n % self.break_every == 0:
            broken = q["gold_sql"].replace("SELECT ", "SELECT no_such_column, ", 1)
            steps.append([tool_use("run_sql", {"sql": broken, "purpose": "first try"})])
        steps.append([tool_use("run_sql", {"sql": q["gold_sql"], "purpose": "answer"})])
        steps.append([tool_use("submit_answer", {"sql": q["gold_sql"], "answer": "See table.",
                                                 "assumptions": []})])
        return steps
