PY ?= python3
VENV := .venv
BIN := $(VENV)/bin
UV := $(shell command -v uv 2>/dev/null)

.PHONY: help install graph tools test lint format-check typecheck smoke check bench-drift bench-agree clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# With uv, install exactly what uv.lock pins (what CI runs). Without it, fall
# back to venv + pip, after checking the interpreter: requires-python is
# >= 3.11 and a plain `python3` is often older. A venv made by uv has no pip,
# so bootstrap it with ensurepip instead of failing on `-m pip`.
$(BIN)/csa: pyproject.toml $(wildcard uv.lock)
ifdef UV
	@$(UV) sync --locked --extra dev
else
	@$(PY) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "csa needs Python >= 3.11: run make PY=python3.11 (or install uv)")'
	@test -d $(VENV) || $(PY) -m venv $(VENV)
	@$(BIN)/python -m pip --version >/dev/null 2>&1 || $(BIN)/python -m ensurepip >/dev/null
	@$(BIN)/python -m pip install -q --upgrade pip
	@$(BIN)/python -m pip install -q -e ".[dev]"
endif
	@touch $(BIN)/csa

install: $(BIN)/csa ## Create the venv and install
	@true

graph: install ## Rank what could explain a scheduling symptom
	@$(BIN)/csa causes slurm.scheduler

tools: install ## Show the read-only tool surface
	@$(BIN)/csa tools

test: install ## Tests with branch coverage (gate in pyproject); no cluster, no API key
	@$(BIN)/python -m pytest -q --cov=csa --cov-branch

lint: install ## ruff
	@$(BIN)/ruff check .

format-check: install ## ruff format --check
	@$(BIN)/ruff format --check .

typecheck: install ## mypy --strict
	@$(BIN)/mypy

smoke: install ## The CLI guard smoke list CI runs
	@CSA=$(BIN)/csa sh scripts/smoke-guard.sh

check: lint format-check typecheck test smoke ## Everything CI runs

bench-drift: install ## Compare the benchmark snapshot with ../slurm-rca-bench (origin/main)
	@$(BIN)/python scripts/sync_bench_snapshot.py ../slurm-rca-bench --ref origin/main --check

# The snapshot is pinned, so CI stays green while the benchmark changes; this
# runs the agreement checks against the sibling checkout's working tree,
# uncommitted edits included, so a contradiction shows up before the re-sync.
bench-agree: install ## Agreement tests against ../slurm-rca-bench's working tree (not a re-sync)
	@CSA_BENCH_DIR=../slurm-rca-bench $(BIN)/python -m pytest -q -rs tests/test_benchmark_agreement.py

clean: ## Remove venv and caches
	@rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache .coverage
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
