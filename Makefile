.PHONY: install lint format test check clean shortlist

install:
	uv sync --extra dev
	uv run pre-commit install

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format:
	uv run ruff check --fix .
	uv run ruff format .

test:
	uv run pytest

check: lint test

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# One day's job-shortlist run. Make stops at the first failing stage, which
# matters: every stage defaults to the newest file from the one before, so a
# failed scrape would otherwise have the rest quietly reprocess yesterday's.
shortlist:
	uv run hiringcafe-toolkit job-shortlist scrape
	uv run hiringcafe-toolkit job-shortlist normalize
	uv run hiringcafe-toolkit job-shortlist screen
	uv run hiringcafe-toolkit job-shortlist diff \
		--visit-log data/inputs/visitlogger-export.json
	uv run hiringcafe-toolkit job-shortlist render \
		--visit-log data/inputs/visitlogger-export.json
