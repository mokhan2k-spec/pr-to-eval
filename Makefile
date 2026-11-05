PYTHON ?= python

.PHONY: setup test validate readme mutation verify-upstream agent-runs llm-grade

setup:
	$(PYTHON) -m pip install -r requirements.txt
	npm ci

test:
	$(PYTHON) -m pytest tests -q

validate:
	$(PYTHON) -m harness.validate --check-corpus --check-readme

readme:
	$(PYTHON) -m harness.validate --write-readme
	$(PYTHON) -m harness.mutate --write-readme

mutation:
	$(PYTHON) -m harness.mutate --check-readme

# Needs network and an authenticated gh. Not run in CI.
verify-upstream:
	$(PYTHON) -m harness.verify_upstream

# Manual only: spends real money, needs the claude and codex CLIs. Never in CI.
agent-runs:
	$(PYTHON) -m harness.agent_runs

# Manual only: calls the claude CLI. Never in CI.
llm-grade:
	$(PYTHON) -m harness.llm_grade
