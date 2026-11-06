# pr-to-eval

[![validate](https://github.com/mokhan2k-spec/pr-to-eval/actions/workflows/validate.yml/badge.svg?branch=main)](https://github.com/mokhan2k-spec/pr-to-eval/actions/workflows/validate.yml)
[![mutation](https://github.com/mokhan2k-spec/pr-to-eval/actions/workflows/mutation.yml/badge.svg?branch=main)](https://github.com/mokhan2k-spec/pr-to-eval/actions/workflows/mutation.yml)

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
| Task | Lang | Upstream PR | Solution | Kind | Hidden tests | Shell checks | LLM grader (recorded) | Verdict | Expected | Match |
|---|---|---|---|---|---|---|---|---|---|---|
| boltons-458 | python | [#458](https://github.com/mahmoud/boltons/pull/458) | reference | reference | 212/212 | 3/3 | PASS | PASS | PASS | yes |
| boltons-458 | python | [#458](https://github.com/mahmoud/boltons/pull/458) | alt-1 | alt | 212/212 | 3/3 | PASS | PASS | PASS | yes |
| boltons-458 | python | [#458](https://github.com/mahmoud/boltons/pull/458) | alt-3 | alt | 212/212 | 3/3 | PASS | PASS | PASS | yes |
| boltons-458 | python | [#458](https://github.com/mahmoud/boltons/pull/458) | flawed-1 | flawed | 196/212 | 3/3 | FAIL | FAIL | FAIL | yes |
| boltons-458 | python | [#458](https://github.com/mahmoud/boltons/pull/458) | flawed-2 | flawed | 205/212 | 3/3 | FAIL | FAIL | FAIL | yes |
| boltons-458 | python | [#458](https://github.com/mahmoud/boltons/pull/458) | flawed-3 | flawed | 183/212 | 3/3 | FAIL | FAIL | FAIL | yes |
| es-toolkit-2170 | typescript | [#2170](https://github.com/toss/es-toolkit/pull/2170) | reference | reference | 17/17 | 3/3 | PASS | PASS | PASS | yes |
| es-toolkit-2170 | typescript | [#2170](https://github.com/toss/es-toolkit/pull/2170) | alt-1 | alt | 17/17 | 3/3 | PASS | PASS | PASS | yes |
| es-toolkit-2170 | typescript | [#2170](https://github.com/toss/es-toolkit/pull/2170) | alt-3 | alt | 17/17 | 3/3 | PASS | PASS | PASS | yes |
| es-toolkit-2170 | typescript | [#2170](https://github.com/toss/es-toolkit/pull/2170) | flawed-1 | flawed | 14/17 | 3/3 | FAIL | FAIL | FAIL | yes |
| es-toolkit-2170 | typescript | [#2170](https://github.com/toss/es-toolkit/pull/2170) | flawed-2 | flawed | 13/17 | 3/3 | FAIL | FAIL | FAIL | yes |
| es-toolkit-2170 | typescript | [#2170](https://github.com/toss/es-toolkit/pull/2170) | flawed-3 | flawed | 13/17 | 3/3 | FAIL | FAIL | FAIL | yes |
| more-itertools-1345 | python | [#1345](https://github.com/more-itertools/more-itertools/pull/1345) | reference | reference | 42/42 | 3/3 | PASS | PASS | PASS | yes |
| more-itertools-1345 | python | [#1345](https://github.com/more-itertools/more-itertools/pull/1345) | alt-1 | alt | 42/42 | 3/3 | PASS | PASS | PASS | yes |
| more-itertools-1345 | python | [#1345](https://github.com/more-itertools/more-itertools/pull/1345) | alt-3 | alt | 42/42 | 3/3 | PASS | PASS | PASS | yes |
| more-itertools-1345 | python | [#1345](https://github.com/more-itertools/more-itertools/pull/1345) | flawed-1 | flawed | 34/42 | 3/3 | FAIL | FAIL | FAIL | yes |
| more-itertools-1345 | python | [#1345](https://github.com/more-itertools/more-itertools/pull/1345) | flawed-2 | flawed | 35/42 | 3/3 | FAIL | FAIL | FAIL | yes |
| more-itertools-1345 | python | [#1345](https://github.com/more-itertools/more-itertools/pull/1345) | flawed-3 | flawed | 38/42 | 3/3 | FAIL | FAIL | FAIL | yes |
| more-itertools-1345 | python | [#1345](https://github.com/more-itertools/more-itertools/pull/1345) | flawed-4 | flawed | 36/42 | 2/3 | FAIL | FAIL | FAIL | yes |

**19/19 solutions match their expected verdict.**
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

- `make boundary-probe`: checks what each agent can reach outside its workspace. No trial or alt launches without a passing probe bound to the current CLI version and command policy.
- `make agent-runs`: runs the trials.
- `make regrade`: re-grades attempts after a grader or auditor change.
- `make confirm ATTEMPT=… SOURCE=… EVIDENCE="…"`: records the reviewer's classification.
- `make interrupted ATTEMPT=… EVIDENCE="…" [STALE=--stale-started]`: records an attempt that died, with an inventory of what was salvaged, so it can be retried.
- `make alt TASK=… AGENT=… LABEL=alt-N [CONSTRAINT="…"]`: makes an independent alternative solution under the same boundary. The evidence goes to `runs/_alts/`.
- `make report` and `make report-check`: write and verify the summary.
- `make llm-grade`: adds the advisory LLM grade.
- `make privacy`: scans the files and the git history before anything is pushed.

**Isolation boundary (a documented limitation).**
- Claude: direct reads and network access outside the workspace by its own tools are denied. File tools are path-scoped, and Bash is limited to an allowlist.
- Codex: network access is denied. Shell reads outside the workspace are detected but not prevented.
- Code the agent runs, such as its own tests: outside reads and network access are detected by the audit (leak check plus a static scan), NOT prevented.

**Codex results are host-configured, NOT independent.** codex-cli loads the host's personal `~/.codex/AGENTS.md`, and no per-run flag turns that off: `--ignore-user-config` and `project_doc_max_bytes=0` were both tried. The audit shows the agent following it. In all 15 Codex trials it read `~/.codex/RTK.md` from outside its workspace. In 9 of 15 it called the host's local `qwen` helper, and in 6 of those `qwen code` drafted code. These counts come from the transcript audit and may undercount. So each Codex result is labelled "host-configured: personal ~/.codex/AGENTS.md loaded, NOT independent". Those results are kept out of the headline figure and out of every independence claim. The Claude trials ran clean and are the headline. The rerun with an empty `CODEX_HOME` is documented in [grading decision A15](docs/grading-decisions.md).

## Grading decisions

Why each grader rule exists and what changed: [docs/grading-decisions.md](docs/grading-decisions.md).

## Licences

Our code is MIT ([LICENSE](LICENSE)). Vendored upstream files keep their own licences: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
