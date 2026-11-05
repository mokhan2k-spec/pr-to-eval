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
| Task | Mutants | Killed | Equivalent (justified) | Unjustified survivors | Score |
|---|---|---|---|---|---|
| boltons-458 | 26 | 26 | 0 | 0 | 100.0% |
| es-toolkit-2170 | 5 | 4 | 1 | 0 | 100.0% |
| more-itertools-1345 | 57 | 54 | 3 | 0 | 100.0% |

**3/3 tasks have no unjustified survivors and no stale equivalent-mutant entries.**
<!-- mutation-table:end -->

## Agent runs

Results and the failure-source breakdown: [runs/SUMMARY.md](runs/SUMMARY.md).

Each task was run 5 times with Claude Code and 5 times with Codex. The full evidence for every attempt is committed under `runs/`: transcript, final patch, grade, audit and the confirmed classification. Agent runs need the two CLIs and a signed-in account, so they are manual and never run in CI. The make targets are:

- `make boundary-probe`: checks what each agent can reach outside its workspace.
- `make agent-runs`: runs the trials.
- `make regrade`: re-grades attempts after a grader or auditor change.
- `make confirm ATTEMPT=… SOURCE=… EVIDENCE="…"`: records the reviewer's classification.
- `make report` and `make report-check`: write and verify the summary.
- `make llm-grade`: adds the advisory LLM grade.
- `make privacy`: scans the files and the git history before anything is pushed.

**Isolation boundary (a documented limitation).**
- Claude: direct reads and network access outside the workspace by its own tools are denied. File tools are path-scoped, and Bash is limited to an allowlist.
- Codex: network access is denied. Shell reads outside the workspace are detected but not prevented.
- Code the agent runs, such as its own tests: outside reads and network access are detected by the audit (leak check plus a static scan), NOT prevented.

## Grading decisions

Why each grader rule exists and what changed: [docs/grading-decisions.md](docs/grading-decisions.md).

## Licences

Our code is MIT ([LICENSE](LICENSE)). Vendored upstream files keep their own licences: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
