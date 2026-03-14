.PHONY: check lint typecheck complexity test

check: lint typecheck complexity test

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run basedpyright

complexity:
	uv run complexipy src/

test:
	uv run pytest
