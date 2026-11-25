# Surplus — developer entry points. `make check` must be green at the end of every phase.
SHELL := /bin/bash
export PATH := $(HOME)/.local/bin:$(PATH)
# The repo may live on a different filesystem than uv's cache (e.g. NTFS); hardlinks fail there.
export UV_LINK_MODE := copy
UV := uv
NPM := npm --prefix frontend

.PHONY: install check lint fmt typecheck test test-backend test-frontend e2e build dev openapi redteam redteam-agent runner-image

install:  ## Install backend + frontend dependencies
	$(UV) sync
	$(NPM) ci

check: lint typecheck test  ## Everything CI runs

lint:
	$(UV) run ruff check backend
	$(UV) run ruff format --check backend

fmt:
	$(UV) run ruff format backend
	$(UV) run ruff check --fix backend

typecheck:
	$(UV) run mypy
	$(NPM) run -s typecheck

test: test-backend test-frontend

test-backend:
	$(UV) run pytest

test-frontend:
	$(NPM) run -s test

e2e:  ## Playwright smoke tests against an isolated backend with a FakeClaude CLI (needs `make build`)
	$(NPM) run -s e2e

openapi:  ## Regenerate openapi.json and the typed frontend client
	$(UV) run surplus openapi
	$(NPM) run -s gen:api

build: openapi  ## Build the SPA into backend/surplus/static (served by FastAPI)
	$(NPM) run -s build

dev:  ## Backend on :8765 + Vite on :5173 (proxying /api)
	$(UV) run surplus serve & $(NPM) run dev

runner-image:  ## Build the agent container image (rootless Podman)
	uv run surplus runner build

redteam:  ## Container isolation suite against real Podman (no Claude quota)
	uv run pytest -m podman backend/tests/redteam -v

redteam-agent:  ## Same attacks, attempted by a real Haiku agent (spends a little quota)
	SURPLUS_REDTEAM_AGENT=1 uv run pytest -m quota backend/tests/redteam -v -s
