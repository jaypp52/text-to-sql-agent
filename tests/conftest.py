from __future__ import annotations

import json

import pytest

from agent.config import ROOT, SEED_DBT_TARGET, SEED_WAREHOUSE, seed_settings
from agent.db import ReadOnlyDB
from agent.schema_index import SchemaIndex
from agent.tools import Toolbox
from seed.make_seed_db import build


@pytest.fixture(scope="session")
def seed_path():
    if not SEED_WAREHOUSE.exists():
        build(SEED_WAREHOUSE)
    return SEED_WAREHOUSE


@pytest.fixture(scope="session")
def db(seed_path):
    d = ReadOnlyDB(seed_path, timeout_s=10)
    yield d
    d.close()


@pytest.fixture(scope="session")
def index(db):
    return SchemaIndex.build(SEED_DBT_TARGET, db)


@pytest.fixture(scope="session")
def toolbox(index, db):
    return Toolbox(index, db)


@pytest.fixture(scope="session")
def settings(seed_path):
    return seed_settings(model="claude-opus-5-5", effort="medium", use_fallback=True)


@pytest.fixture(scope="session")
def questions():
    path = ROOT / "eval" / "questions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
