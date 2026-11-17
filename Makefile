.PHONY: install test lint extract status dbt match evaluate pipeline

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check . && uv run mypy src

extract:
	uv run recon extract erp

status:
	uv run recon status

# dbt runs from the repo root so the DuckDB path matches the CLI's (.env RECON_DUCKDB_PATH)
DBT = uv run --group dbt dbt
dbt:
	$(DBT) build --project-dir dbt --profiles-dir dbt

match:
	uv run recon match

evaluate:
	uv run recon evaluate

# Full refresh from the live sandboxes: extract -> dbt -> match -> score
pipeline:
	uv run recon extract erp && uv run recon extract stripe && uv run recon extract bank
	$(MAKE) dbt
	uv run recon match && uv run recon evaluate
