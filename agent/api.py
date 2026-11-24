"""HTTP API.

    uvicorn agent.api:app --port 8000
    curl -s localhost:8000/ask -H 'content-type: application/json' \
         -d '{"question": "How many devices are in us-west?"}'
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from agent.config import load_settings
from agent.db import to_jsonable
from agent.loop import Agent

STATE: dict = {}


def get_agent() -> Agent:
    if "agent" not in STATE:
        STATE["agent"] = Agent(load_settings())
        STATE["owned"] = True  # created here, so closed here on shutdown
    return STATE["agent"]


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    if STATE.pop("owned", False):
        STATE.pop("agent").db.close()


app = FastAPI(title="Text-to-SQL Agent", version="0.1.0", lifespan=lifespan)


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    effort: Literal["low", "medium", "high"] | None = None


class AskResponse(BaseModel):
    status: str
    answer: str
    sql: str
    columns: list[str]
    rows: list[list]
    row_count: int
    assumptions: list[str]
    error: str | None
    trace: list[dict]
    usage: dict
    cost_usd: float | None
    latency_ms: float


@app.get("/health")
def health() -> dict:
    a = get_agent()
    return {"ok": True, "model": a.s.model, "effort": a.s.effort,
            "warehouse": "seed" if a.s.using_seed else "platform",
            "tables": [d.fqn for d in a.index.docs]}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    agent = get_agent()
    if req.effort and req.effort != agent.s.effort:
        agent = Agent(agent.s.with_(effort=req.effort), client=agent.client, db=agent.db,
                      index=agent.index)
    res = agent.ask(req.question)
    if res.error_kind == "api_error":
        raise HTTPException(status_code=502, detail=res.error)
    return AskResponse(
        status=res.status, answer=res.answer, sql=res.sql, columns=res.columns,
        rows=[[to_jsonable(v) for v in r] for r in res.rows[:200]], row_count=res.row_count,
        assumptions=res.assumptions, error=res.error,
        trace=[{k: to_jsonable(v) for k, v in t.items()} for t in res.trace],
        usage=res.usage, cost_usd=res.cost_usd, latency_ms=res.latency_ms,
    )
