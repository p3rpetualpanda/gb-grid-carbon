# GB Grid Carbon Tracker: convenience commands (Linux/macOS/CI; Windows: see README).
# Usage: make <target>

PY ?= python

.PHONY: help setup lint format test check init-db backfill incremental status fixtures backtest

help:
	@echo "setup        - install the package + dev tools (editable)"
	@echo "lint         - ruff lint + format check"
	@echo "format       - apply ruff fixes and formatting"
	@echo "test         - run the offline test suite"
	@echo "check        - lint + test (what CI runs)"
	@echo "init-db      - create warehouse schemas and tables"
	@echo "backfill     - load full history (resumable)"
	@echo "incremental  - bring all datasets up to date + snapshot forecasts"
	@echo "status       - row counts / coverage / latest runs"
	@echo "fixtures     - re-record API responses used by the tests"
	@echo "backtest     - re-run the backtest and copy its numbers into the docs"

setup:
	$(PY) -m pip install -e ".[dev,dbt,ml,analysis]"
	pre-commit install

lint:
	$(PY) -m ruff check src tests scripts analysis app
	$(PY) -m ruff format --check src tests scripts analysis app

format:
	$(PY) -m ruff check src tests scripts analysis app --fix
	$(PY) -m ruff format src tests scripts analysis app

test:
	$(PY) -m pytest

check: lint test

init-db:
	gridcarbon init-db

backfill:
	gridcarbon backfill

incremental:
	gridcarbon incremental

status:
	gridcarbon status

fixtures:
	$(PY) scripts/record_fixtures.py

backtest:
	$(PY) analysis/backtest.py
	$(PY) scripts/update_backtest_docs.py
