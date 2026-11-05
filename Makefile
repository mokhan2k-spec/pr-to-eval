PYTHON ?= python

.PHONY: setup test validate readme mutation verify-upstream boundary-probe agent-runs regrade confirm report report-check llm-grade privacy

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
boundary-probe:
	$(PYTHON) -m harness.boundary_probe

agent-runs:
	$(PYTHON) -m harness.agent_runs

# Re-audit and re-grade attempts whose patch, task, grader or auditor changed.
regrade:
	$(PYTHON) -m harness.agent_runs regrade

# Confirm a classification: make confirm ATTEMPT=runs/<task>/<agent>/trial-<n>/attempt-<k> SOURCE=agent EVIDENCE="..."
confirm:
	$(PYTHON) -m harness.agent_runs confirm $(ATTEMPT) --source $(SOURCE) --evidence "$(EVIDENCE)"

report:
	$(PYTHON) -m harness.report --write

report-check:
	$(PYTHON) -m harness.report --check

# Manual only: calls the claude CLI. Never in CI.
llm-grade:
	$(PYTHON) -m harness.llm_grade

privacy:
	$(PYTHON) -m harness.privacy_check --history
