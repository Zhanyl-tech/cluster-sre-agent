PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: help install graph tools test lint typecheck check clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

$(BIN)/csa: pyproject.toml
	@test -d $(VENV) || $(PY) -m venv $(VENV)
	@$(BIN)/python -m pip install -q --upgrade pip
	@$(BIN)/python -m pip install -q -e ".[dev]"
	@touch $(BIN)/csa

install: $(BIN)/csa ## Create the venv and install

graph: install ## Rank what could explain a scheduling symptom
	@$(BIN)/csa causes slurm.scheduler

tools: install ## Show the read-only tool surface
	@$(BIN)/csa tools

test: install ## Run the test suite (no cluster, no API key)
	@$(BIN)/python -m pytest -q

lint: install ## ruff
	@$(BIN)/ruff check .

typecheck: install ## mypy --strict
	@$(BIN)/mypy

check: lint typecheck test ## Everything CI runs

clean: ## Remove venv and caches
	@rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
