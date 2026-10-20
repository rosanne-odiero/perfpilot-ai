SHELL := /bin/bash
UV ?= uv
API_HOST ?= 127.0.0.1
API_PORT ?= 8000
UI_PORT ?= 8501
SANDBOX_IMAGE ?= perfpilot-sandbox:latest
PYTHON ?= $(UV) run python
COV_FAIL_UNDER ?= 92
# docker compose bind-mounts this directory at the same absolute path in every service.
export PERFPILOT_HOST_DATA_DIR ?= $(CURDIR)/data

.PHONY: help install api worker ui dev check lint format typecheck test test-fast cov clean \
	sandbox-image test-docker init-db migration migration-check migration-baseline \
	compose-build compose-up compose-down compose-logs openapi perf

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies (incl. dev) from uv.lock
	$(UV) sync --all-extras

api: ## Run the FastAPI server
	$(UV) run uvicorn backend.main:app --host $(API_HOST) --port $(API_PORT) --reload

worker: ## Run the persistent job worker
	$(UV) run python -m backend.jobs.worker

ui: ## Run the Streamlit frontend
	$(UV) run streamlit run frontend/app.py --server.port $(UI_PORT)

dev: ## Run API, worker and UI together (Ctrl+C stops all)
	@trap 'kill 0' INT TERM EXIT; \
	$(MAKE) --no-print-directory api & \
	$(MAKE) --no-print-directory worker & \
	$(MAKE) --no-print-directory ui & \
	wait

lint: ## Ruff lint + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format: ## Auto-format and fix lint
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck: ## mypy (strict) on backend
	$(UV) run mypy

test: ## Full test suite
	$(UV) run pytest

test-fast: ## Tests excluding slow/docker/network
	$(UV) run pytest -m "not slow and not docker and not network"

cov: ## Tests with coverage (fails under COV_FAIL_UNDER, like CI)
	$(UV) run pytest -m "not docker and not network" --cov --cov-report=term-missing \
		--cov-report=xml --cov-fail-under=$(COV_FAIL_UNDER)

perf: ## Performance smoke tests (non-authoritative)
	$(UV) run pytest tests/performance -ra

init-db: ## Apply database migrations (alembic upgrade head)
	$(UV) run perfpilot init-db

migration: ## Autogenerate the next migration: make migration m="add foo table"
	@test -n "$(m)" || { echo 'usage: make migration m="message"'; exit 2; }
	$(PYTHON) -m backend.models.migrations revision -m "$(m)"

migration-check: ## Fail if the ORM metadata differs from the migration head
	$(PYTHON) -m backend.models.migrations check

migration-baseline: ## Regenerate 0001 from the ORM (only while it is the sole revision)
	$(PYTHON) -m backend.models.migrations baseline
	$(PYTHON) -m backend.models.migrations check

openapi: ## Regenerate docs/openapi.json and docs/api.md from the FastAPI app
	$(PYTHON) scripts/export_openapi.py

sandbox-image: ## Build the runtime sandbox image (needs Docker)
	docker build --file docker/sandbox.Dockerfile --tag $(SANDBOX_IMAGE) \
		$(if $(PIP_INDEX_URL),--build-arg PIP_INDEX_URL=$(PIP_INDEX_URL),) docker

compose-build: ## Build the api/worker/ui images (UV_INDEX_URL=... for a package mirror)
	mkdir -p "$(PERFPILOT_HOST_DATA_DIR)"
	docker compose build

compose-up: ## Start api + worker + ui with docker compose (data in PERFPILOT_HOST_DATA_DIR)
	mkdir -p "$(PERFPILOT_HOST_DATA_DIR)"
	docker compose up --build -d

compose-down: ## Stop the compose stack (data is kept)
	docker compose down

compose-logs: ## Follow the compose logs
	docker compose logs -f

test-docker: ## Sandbox integration tests (need Docker + the sandbox image)
	$(UV) run pytest -m docker -ra

check: lint typecheck migration-check test-fast ## Everything CI runs (minus docker/network)

clean: ## Remove caches (keeps data/)
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov coverage.xml
