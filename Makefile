.PHONY: install test lint extract status

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
