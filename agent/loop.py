"""The agent: a hand-written Claude tool-use loop.

Written by hand on purpose (no framework, not the SDK tool runner) so every step is
visible, bounded and testable: stop reasons are checked before content is read, every
tool_use block of a turn is answered in ONE user message, failed tools come back with
``is_error: true``, the number of failed SQL attempts is capped, and the submitted SQL is
re-executed before the answer is accepted.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field

import anthropic

from agent.config import FALLBACK_BETA, FALLBACK_MODELS, PRICING, Settings
from agent.db import ReadOnlyDB
from agent.prompts import NUDGE, system_prompt
from agent.schema_index import SchemaIndex
from agent.tools import Toolbox, tool_definitions

log = logging.getLogger(__name__)

FAILED_KINDS = {"sql_error", "guard_rejection", "timeout"}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    api_calls: int = 0

    def add(self, u) -> None:
        parts = getattr(u, "iterations", None) or [u]
        for p in parts:
            self.input_tokens += getattr(p, "input_tokens", 0) or 0
            self.output_tokens += getattr(p, "output_tokens", 0) or 0
            self.cache_read_input_tokens += getattr(p, "cache_read_input_tokens", 0) or 0
            self.cache_creation_input_tokens += getattr(p, "cache_creation_input_tokens", 0) or 0
        self.api_calls += 1

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens

    def cost(self, model: str) -> float | None:
        p = PRICING.get(model)
        if p is None:
            return None
        return (
            self.input_tokens * p["input"]
            + self.output_tokens * p["output"]
            + self.cache_creation_input_tokens * p["cache_write"]
            + self.cache_read_input_tokens * p["cache_read"]
        ) / 1e6


@dataclass
class AgentResult:
    question: str
    status: str = "error"  # answered | abstained | error
    answer: str = ""
    sql: str = ""
    assumptions: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    error: str | None = None
    error_kind: str | None = None
    turns: int = 0
    tool_calls: int = 0
    sql_attempts: list[dict] = field(default_factory=list)
    guard_rejections: int = 0
    fallback_used: bool = False
    model: str = ""
    effort: str = ""
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    latency_ms: float = 0.0
    trace: list[dict] = field(default_factory=list)

    @property
    def sql_errors(self) -> int:
        return sum(1 for a in self.sql_attempts if not a["ok"])

    @property
    def first_sql_ok(self) -> bool | None:
        return self.sql_attempts[0]["ok"] if self.sql_attempts else None

    @property
    def retries(self) -> int:
        a = self.sql_attempts
        return sum(1 for i in range(1, len(a)) if not a[i - 1]["ok"])

    @property
    def recovered(self) -> bool:
        return self.sql_errors > 0 and self.status == "answered"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(sql_errors=self.sql_errors, first_sql_ok=self.first_sql_ok,
                 retries=self.retries, recovered=self.recovered)
        return d


def _preview(text: str, n: int = 600) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _echo(content: list) -> list:
    """Content to append to the history. After a server-side fallback, drop the
    thinking/tool_use blocks that precede the last ``fallback`` block (as the API requires);
    everything else is echoed back unchanged."""
    idx = [i for i, b in enumerate(content) if getattr(b, "type", None) == "fallback"]
    if not idx:
        return list(content)
    last = idx[-1]
    drop = {"thinking", "redacted_thinking", "tool_use"}
    return [b for i, b in enumerate(content) if i >= last or getattr(b, "type", None) not in drop]


class Agent:
    def __init__(self, settings: Settings, client=None, db: ReadOnlyDB | None = None,
                 index: SchemaIndex | None = None):
        self.s = settings
        schemas = ("marts", "staging") if settings.expose_staging else ("marts",)
        self.db = db or ReadOnlyDB(settings.warehouse_path, schemas, settings.query_timeout_s)
        self.index = index or SchemaIndex.build(settings.dbt_target_dir, self.db,
                                                expose_staging=settings.expose_staging)
        self.toolbox = Toolbox(self.index, self.db, settings.result_rows, settings.row_limit,
                               settings.error_chars)
        self.client = client if client is not None else anthropic.Anthropic(max_retries=4)
        self.tools = tool_definitions(settings.schema_retrieval)
        prompt = system_prompt(settings.schema_retrieval, self.index.all_schemas_text()
                               if not settings.schema_retrieval else "")
        # Breakpoint on the last system block caches tools + system (identical every call).
        self.system = [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]

    # -- API call ----------------------------------------------------------------------
    def _create(self, messages: list):
        params = dict(
            model=self.s.model,
            max_tokens=self.s.max_tokens,
            system=self.system,
            tools=self.tools,
            messages=messages,
            output_config={"effort": self.s.effort},
            # automatic breakpoint: moves forward with the conversation, so each turn
            # reads the previous turns from cache
            cache_control={"type": "ephemeral"},
        )
        if self.s.use_fallback and self.s.model in FALLBACK_MODELS:
            return self.client.beta.messages.create(
                **params, betas=[FALLBACK_BETA], fallbacks="default"
            )
        return self.client.messages.create(**params)

    # -- loop --------------------------------------------------------------------------
    def ask(self, question: str) -> AgentResult:
        s = self.s
        r = AgentResult(question=question, model=s.model, effort=s.effort)
        usage = Usage()
        messages: list = [{"role": "user", "content": question}]
        nudged = False
        t_start = time.perf_counter()

        def finish(status: str, error: str | None = None, kind: str | None = None):
            r.status, r.error, r.error_kind = status, error, kind
            r.usage = asdict(usage)
            r.cost_usd = usage.cost(s.model)
            r.latency_ms = round((time.perf_counter() - t_start) * 1000, 1)
            return r

        for turn in range(1, s.max_turns + 1):
            r.turns = turn
            t0 = time.perf_counter()
            try:
                resp = self._create(messages)
            except anthropic.APIError as e:
                return finish("error", f"API error: {e}", "api_error")
            usage.add(resp.usage)
            iterations = getattr(resp.usage, "iterations", None) or []
            if any(getattr(it, "type", "") == "fallback_message" for it in iterations):
                r.fallback_used = True
            r.trace.append({
                "turn": turn, "type": "api", "stop_reason": resp.stop_reason,
                "model": getattr(resp, "model", s.model),
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "usage": {k: getattr(resp.usage, k, None) for k in (
                    "input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens")},
            })

            # stop_reason first, before reading content
            if resp.stop_reason == "refusal":
                details = getattr(resp, "stop_details", None)
                cat = getattr(details, "category", None) if details else None
                return finish("error", f"the model declined to answer (category: {cat})",
                              "refusal")
            if resp.stop_reason == "max_tokens":
                return finish("error", f"response hit max_tokens={s.max_tokens}; raise "
                              "CLAUDE_MAX_TOKENS", "max_tokens")

            for b in resp.content:
                if b.type == "text" and b.text.strip():
                    r.trace.append({"turn": turn, "type": "text", "text": _preview(b.text, 2000)})

            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": _echo(resp.content)})
                continue

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason == "end_turn" or not tool_uses:
                if nudged:
                    return finish("error", "the model stopped without calling submit_answer",
                                  "no_submit")
                nudged = True
                messages.append({"role": "assistant", "content": _echo(resp.content)})
                messages.append({"role": "user", "content": NUDGE})
                r.trace.append({"turn": turn, "type": "nudge", "text": NUDGE})
                continue

            messages.append({"role": "assistant", "content": _echo(resp.content)})
            results = []
            submit = None
            for b in tool_uses:
                r.tool_calls += 1
                if b.name == "submit_answer":
                    submit = b
                    continue
                out = self.toolbox.call(b.name, b.input)
                step = {"turn": turn, "type": "tool_use", "tool": b.name, "input": b.input,
                        "is_error": out.is_error, "kind": out.kind,
                        "output": _preview(out.content), "ms": out.elapsed_ms}
                r.trace.append(step)
                if b.name == "run_sql":
                    r.sql_attempts.append({"sql": b.input.get("sql", ""), "ok": not out.is_error,
                                           "kind": out.kind, "via": "run_sql"})
                    if out.kind == "guard_rejection":
                        r.guard_rejections += 1
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": out.content, "is_error": out.is_error})

            if submit is not None:
                done, result = self._handle_submit(submit, r, turn)
                if done:
                    return finish(result)
                results.append(result)

            if r.sql_errors >= s.max_sql_errors:
                last = next(a for a in reversed(r.sql_attempts) if not a["ok"])
                err = next((t["output"] for t in reversed(r.trace)
                            if t.get("type") in ("tool_use", "submit") and t.get("is_error")), "")
                return finish("error", f"stopped after {r.sql_errors} failed SQL attempts; "
                              f"last error: {err}", f"sql_budget:{last['kind']}")
            messages.append({"role": "user", "content": results})

        return finish("error", f"no answer within {s.max_turns} turns", "max_turns")

    def _handle_submit(self, block, r: AgentResult, turn: int):
        """Validate submit_answer. Returns (True, status) when accepted, else
        (False, tool_result) to send the problem back to the model."""
        args = block.input if isinstance(block.input, dict) else {}
        sql = (args.get("sql") or "").strip()
        r.answer = str(args.get("answer", ""))
        r.assumptions = [str(a) for a in (args.get("assumptions") or [])]
        r.sql = sql
        step = {"turn": turn, "type": "submit", "tool": "submit_answer", "input": args,
                "is_error": False}
        r.trace.append(step)
        if not sql:
            return True, "abstained"
        out = self.toolbox.run_sql(sql)
        r.sql_attempts.append({"sql": sql, "ok": not out.is_error, "kind": out.kind,
                               "via": "submit"})
        if out.is_error:
            if out.kind == "guard_rejection":
                r.guard_rejections += 1
            step.update(is_error=True, kind=out.kind, output=_preview(out.content))
            msg = ("The SQL in submit_answer failed when re-executed, so the answer was not "
                   f"accepted: {out.content}. Fix it and call submit_answer again.")
            return False, {"type": "tool_result", "tool_use_id": block.id, "content": msg,
                           "is_error": True}
        r.columns, r.rows = out.data["columns"], out.data["rows"]
        r.row_count = out.data["row_count"]
        step["output"] = f"{r.row_count} rows"
        return True, "answered"
