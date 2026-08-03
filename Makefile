.PHONY: check test lint format install

# The full quality gate — run on every change (ruff + mypy + 100% coverage).
check:
	python scripts/check.py

test:
	pytest -q

lint:
	ruff check . && ruff format --check .

format:
	ruff format .

install:
	pip install -e ".[dev]"
