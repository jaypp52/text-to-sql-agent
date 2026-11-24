# Text-to-SQL agent. `make help` lists the targets.
PY      ?= .venv/bin/python
Q       ?= Which firmware version has the highest crash rate per 100 device-days?
CONFIGS ?= main,effort_low,effort_high,no_retrieval

.PHONY: help setup seed dbt-docs ask demo schema sql eval eval-quick eval-mock gold \
        verify-questions test lint serve clean

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

setup: ## create .venv and install pinned dependencies
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements-dev.txt
	@[ -f .env ] || (cp .env.example .env && echo "created .env - add ANTHROPIC_API_KEY")

seed: ## build the small same-schema seed warehouse (CI / before the platform exists)
	$(PY) -m seed.make_seed_db

dbt-docs: ## re-export the platform's dbt manifest/catalog into seed/dbt_target
	$(PY) -m seed.export_dbt_docs

ask: ## ask one question with the full trace: make ask Q="..."
	$(PY) -m agent.cli ask "$(Q)" --trace

demo: ## 5 sample questions end to end; traces written to docs/sample_runs.md
	$(PY) -m agent.cli demo

schema: ## what search_schema returns: make schema Q="crash rate by firmware"
	$(PY) -m agent.cli schema "$(Q)"

eval: ## full eval (main, effort sweep, retrieval ablation) -> eval/results + README
	$(PY) -m eval.run --configs $(CONFIGS) --readme

eval-quick: ## main config only
	$(PY) -m eval.run --configs main

eval-mock: ## offline pipeline check on the seed with the gold-replay client (CI)
	$(PY) -m eval.run --client mock --seed --configs main,no_retrieval

gold: ## run every gold SQL on the platform warehouse -> eval/gold_results.json
	$(PY) -m eval.gold

verify-questions: ## hand-check each question's gold SQL and result (records verified_by)
	$(PY) -m eval.verify

test: ## pytest (no API key needed)
	$(PY) -m pytest

lint: ## ruff
	$(PY) -m ruff check .

serve: ## FastAPI on :8000 (POST /ask)
	$(PY) -m uvicorn agent.api:app --host 127.0.0.1 --port 8000

clean: ## remove caches, the seed DB and mock results
	rm -rf .pytest_cache .ruff_cache seed/warehouse_seed.duckdb eval/results/mock_* eval/results/runs
	find . -name __pycache__ -prune -exec rm -rf {} +
