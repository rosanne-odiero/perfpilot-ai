SHELL := /bin/bash
UV ?= uv
API_HOST ?= 127.0.0.1
API_PORT ?= 8000
UI_PORT ?= 8501
SANDBOX_IMAGE ?= perfpilot-sandbox:latest

.PHONY: help install api worker ui dev check lint format typecheck test test-fast cov clean sandbox-image test-docker

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

cov: ## Tests with coverage
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=xml

sandbox-image: ## Build the runtime sandbox image (needs Docker)
	docker build --file docker/sandbox.Dockerfile --tag $(SANDBOX_IMAGE) docker

test-docker: ## Sandbox integration tests (need Docker + the sandbox image)
	$(UV) run pytest -m docker -ra

check: lint typecheck test-fast ## Everything CI runs (minus docker/network)

clean: ## Remove caches (keeps data/)
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov coverage.xml
