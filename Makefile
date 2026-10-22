.PHONY: install dev migrate seed test lint
install:
	python3 -m venv .venv
	.venv/bin/pip install -c requirements.lock -e '.[dev]'
dev:
	.venv/bin/uvicorn anymob.main:app --reload --port 8000
migrate:
	.venv/bin/alembic upgrade head
seed:
	.venv/bin/anymob seed
test:
	.venv/bin/pytest -q
lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
