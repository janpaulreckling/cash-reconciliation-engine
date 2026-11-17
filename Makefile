.PHONY: install test lint extract status dbt

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
