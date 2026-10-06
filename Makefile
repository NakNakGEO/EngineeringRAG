# Engineering Intelligence OS - task runner. `make help` lists targets.
.DEFAULT_GOAL := help
SHELL := /bin/bash

ifneq (,$(wildcard .env))
include .env
export
endif

EIOS_POSTGRES_PORT ?= 5432
# Admin URL used only by the integration-test fixture; it creates and drops throwaway databases
# on the Engineering OS PostgreSQL. Never point this anywhere else (the policy would refuse it).
EIOS_TEST_DATABASE_URL ?= postgresql+psycopg://$(EIOS_POSTGRES_USER):$(EIOS_POSTGRES_PASSWORD)@localhost:$(EIOS_POSTGRES_PORT)/postgres

.PHONY: help init sync lint format typecheck forbid-deps test test-unit test-integration check \
        up down logs ps db-up migrate migrate-down migrate-sql revision

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-18s %s\n",$$1,$$2}'

init: ## Create .env from .env.example (does not overwrite)
	@test -f .env || (cp .env.example .env && echo "created .env - review it before 'make up'")

sync: ## Install the workspace (all packages + dev tools) with uv
	uv sync

lint: ## ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

format: ## Auto-format and auto-fix lint
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## mypy --strict
	uv run mypy

forbid-deps: ## Fail if an external-database driver or V1-excluded dependency is present
	uv run python scripts/check_forbidden_deps.py

test-unit: ## Unit + security tests (no database needed)
	uv run pytest -m "not integration"

db-up: ## Start only the Engineering OS PostgreSQL and wait until healthy
	docker compose up -d --wait postgres

test-integration: db-up ## Integration tests against throwaway databases on the compose PostgreSQL
	EIOS_REQUIRE_DB_TESTS=1 EIOS_TEST_DATABASE_URL='$(EIOS_TEST_DATABASE_URL)' uv run pytest -m integration

test: db-up ## Run the whole test suite
	EIOS_REQUIRE_DB_TESTS=1 EIOS_TEST_DATABASE_URL='$(EIOS_TEST_DATABASE_URL)' uv run pytest

check: lint typecheck forbid-deps test ## Everything CI runs

up: ## Build and start the full stack (postgres, migrate, api, worker, mcp)
	docker compose up -d --build --wait

down: ## Stop the stack (keeps data volume)
	docker compose down

logs: ## Follow logs
	docker compose logs -f

ps: ## Show service status
	docker compose ps

migrate: ## Apply migrations (host-run; needs `make db-up`)
	uv run alembic upgrade head

migrate-down: ## Roll back ONE migration (host-run)
	uv run alembic downgrade -1

migrate-sql: ## Print the SQL of pending migrations without executing (offline mode)
	uv run alembic upgrade head --sql

revision: ## Create a new migration: make revision m="add something"
	uv run alembic revision -m "$(m)"
