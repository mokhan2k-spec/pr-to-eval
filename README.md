# pr-to-eval

pr-to-eval turns real merged pull requests from public repos into agent-coding eval tasks. It then shows the graders are sound: they accept the upstream fix and an independently written correct fix, and they reject deliberately flawed fixes for the stated reason. The same graders score repeated Claude Code and Codex runs, and every failure is classified as agent, environment or grader. The build contract is in [DESIGN.md](DESIGN.md).

## How to reproduce

```
git clone <this repo>
cd pr-to-eval
make setup
make validate
make mutation
```

Needs Python 3.11+, Node 22, git and bash. `make verify-upstream` (vendored files and reference patches against the upstream PRs) needs network and an authenticated `gh`, so it is not run in CI.

## What is validated

Every solution of every task is graded by the hidden tests and shell checks, with no LLM in the verdict. The reference and the independent alternative must pass; each flawed solution must fail for its stated reason.

<!-- validation-table:start -->
<!-- validation-table:end -->

Mutation testing: small mistakes are injected into the reference solution and the hidden tests must catch them. Survivors must be listed in the task's `equivalent_mutants.json` with a reason.

<!-- mutation-table:start -->
<!-- mutation-table:end -->

## Agent runs

Results and the failure-source breakdown: [runs/SUMMARY.md](runs/SUMMARY.md).

## Grading decisions

Why each grader rule exists and what changed: [docs/grading-decisions.md](docs/grading-decisions.md).

## Licences

Our code is MIT ([LICENSE](LICENSE)). Vendored upstream files keep their own licences: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
