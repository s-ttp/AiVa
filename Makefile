# Common developer commands. See CONTRIBUTING.md for the full workflow.
.PHONY: setup services migrate seed dev test lint typecheck policy-test contracts web-install web-check check

setup:            ## Install Python (uv) and web (pnpm) dependencies
	uv sync --all-packages --python 3.12
	pnpm -C web install --frozen-lockfile

services:         ## Start Postgres, Redis and OPA locally (no Docker needed)
	bash scripts/dev-services.sh

migrate:
	uv run aiva-admin migrate

seed:             ## Load config/ into the database and publish policies to OPA
	uv run aiva-admin seed

dev:              ## Run the gateway with reload on :8000
	uv run uvicorn --factory aiva_gateway.main:create_app --reload --no-proxy-headers --port 8000

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

policy-test:
	opa fmt --fail --list policies/
	opa test policies/ -v

contracts:        ## Regenerate contracts/ from the gateway's API models
	uv run aiva-admin export-contracts

web-install:
	pnpm -C web install --frozen-lockfile

web-check:
	pnpm -C web typecheck
	pnpm -C web lint
	pnpm -C web test
	pnpm -C web build

check: lint typecheck policy-test test web-check   ## Everything CI runs
