.PHONY: check lint typecheck test

check: lint typecheck test

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run basedpyright

test:
	uv run pytest
