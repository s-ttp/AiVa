# Contributing to AiVa

Thank you for helping. This guide covers local setup, the checks every change must pass, and the conventions the
code follows.

## Local services

The gateway needs PostgreSQL with pgvector, Redis and Open Policy Agent. Either start them without Docker:

```bash
bash scripts/dev-services.sh          # Postgres :5432, Redis :6379, OPA :8181
```

or run the whole stack with `docker compose -f deploy/compose.yaml up --build`.

Tests use the database `aiva_test` and Redis database 15; override with `AIVA_TEST_DATABASE_URL`,
`AIVA_TEST_REDIS_URL` and `AIVA_TEST_OPA_URL`.

## Setup and checks

```bash
make setup                            # uv sync --all-packages --python 3.12; pnpm install
make migrate seed                     # schema, sample tenant, policy bundle
make check                            # lint, mypy --strict, opa fmt/test, pytest, web typecheck/lint/test/build
pnpm -C web/apps/control-center e2e   # Playwright + axe against a local gateway (see its e2e/README.md)
```

A pull request must pass `make check` and CI. CI never calls a model provider: tests run on simulated inference
and recorded provider exchanges (`replayed`).

## Conventions

- **Python:** 3.12, `ruff` (line length 110), `mypy --strict`, async SQLAlchemy 2, Pydantic v2.
- **API models** live in `gateway/src/aiva_gateway/contracts/`. After changing one, run `make contracts` and
  `pnpm -C web gen:api`, and commit the regenerated files.
- **Schema changes:** one Alembic migration per pull request
  (`uv run alembic -c gateway/alembic.ini revision --autogenerate`); never edit a merged migration;
  `uv run alembic -c gateway/alembic.ini check` must pass.
- **Configuration** changes go in `config/*.yaml`; the loader validates cross-references.
- **UI copy** goes through i18n keys, and styles use CSS logical properties, so the Control Center stays
  Arabic-ready.
- **Data:** synthetic only. Never commit secrets or real personal data; read secrets from environment variables.

## Design guarantees

The rules in [README.md](README.md#design-guarantees) have automated tests in `gateway/tests/invariants/` and in
`policies/`. A change must never weaken or skip them. In particular:

- policy runs before optimisation and before every downstream call;
- confidential, restricted and sovereign-only data never reaches an external provider or context service, and the
  sovereign path never falls back;
- no prompt text, PII or secrets in spans, run records or reporting tables;
- every number carries its provenance, and money is integer micro-USD.

If a feature seems to need an exception, open an issue to discuss it first. Lasting design decisions are recorded
as a new file in [`docs/adr/`](docs/adr/).

## Contributor licence

AiVa is licensed under the [Apache License 2.0](LICENSE). Before your first pull request can be merged, you will be
asked to agree to the project's Contributor License Agreement (CLA). It confirms that you have the right to
contribute your change and grants the project the rights it needs to distribute it.

## Reporting security issues

Please don't open public issues for vulnerabilities; see [SECURITY.md](SECURITY.md).
