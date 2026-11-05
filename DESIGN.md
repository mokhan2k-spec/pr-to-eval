# DESIGN — pr-to-eval (the build contract)

pr-to-eval turns real merged pull requests from public repos into agent-coding eval tasks. It then shows the graders are sound: they accept the upstream fix and an independently written correct fix, and they reject deliberately flawed fixes for the stated reason. The same graders then score repeated Claude Code and Codex runs, and every failure is classified as **agent**, **environment** or **grader**.

Everything in this file is a contract. Builders must follow the layout and the schemas exactly, because several people build in parallel.

## Layout

```
pr-to-eval/
  README.md                 validation + mutation tables between markers (generated, CI-checked)
  DESIGN.md                 this file
  LICENSE                   MIT (our code)
  THIRD_PARTY_NOTICES.md    one section per vendored upstream: repo, licence, base SHA, licence text path
  Makefile                  setup | test | validate | mutation | readme | verify-upstream | agent-runs | llm-grade
  requirements.txt          pinned: pytest
  package.json, package-lock.json   pinned devDeps: vitest, typescript (+ @types/node)
  .gitattributes            "* text=auto eol=lf" and "*.patch -text"
  .gitignore                .work/ node_modules/ __pycache__/ .pytest_cache/ *.pyc junit*.xml
  harness/                  Python 3.11+, stdlib only (pytest is used only as a subprocess)
    tasks.py                load + validate task.json / solutions.json
    workspace.py            make_workspace(task, patch=None) -> Path  (copy upstream/ into <repo>/.work/<uuid>/, apply patch with `git apply -p1 --whitespace=nowarn`)
    grade.py                grade(task, workspace) -> dict   (hidden tests + shell checks)
    validate.py             grade every solution of every task, compare with expectations, render table
    mutate.py               mutation testing of the hidden tests against the reference solution
    readme.py               replace text between README markers; --check mode fails on any difference
    verify_upstream.py      network: vendored files == upstream at base_sha; reference.patch == upstream PR source hunks
    agent_runs.py           (manual, never in CI) run Claude Code / Codex N times per task, store logs, grade
    llm_grade.py            (manual, never in CI) run each task's LLM grading prompt via the `claude` CLI
  tests/                    pytest unit tests for the harness (fast, no network, no agents)
  tasks/<task-id>/          see "Task directory"
  runs/<task-id>/<agent>-<n>/   committed agent-run evidence (see "Agent runs")
  runs/SUMMARY.md           generated from the run directories
  docs/grading-decisions.md the grading-decision log (why each grader rule exists, what changed and why)
  .github/workflows/validate.yml   on push + workflow_dispatch: make validate
  .github/workflows/mutation.yml   on push + workflow_dispatch: make mutation
```

## Task directory: `tasks/<task-id>/`

```
task.json
TASK.md              the prompt the agent sees, written like a GitHub issue. States the bug and the required behaviour. Never hints at the fix.
upstream/            vendored files exactly as at base_sha (byte-identical), only what is needed + the upstream LICENSE file.
                     Includes the upstream's own visible tests for the touched area, so an agent can run them.
hidden/              hidden tests, never shown to agents. Copied into the workspace at task.json "hidden_dest" before testing.
checks.sh            shell checks. Usage: bash checks.sh <workspace> <pristine-upstream-dir>. One check per line of output:
                     "PASS <name>" or "FAIL <name>: <reason>". Exit 0 only if all pass. Must not need network.
grader_prompt.md     LLM grading prompt. Placeholders {{TASK}} and {{DIFF}}. Asks for JSON only:
                     {"verdict": "PASS"|"FAIL", "correctness": 0-3, "scope": 0-3, "reasons": "..."}
solutions/
  solutions.json
  reference.patch    the upstream merged PR's change to source files only (no test/doc/changelog hunks), paths relative to upstream/ (a/… b/…)
  alt-1.patch        independent correct implementation (written WITHOUT seeing reference.patch or hidden/)
  flawed-1.patch …   ≥3 deliberately flawed implementations, each a plausible mistake a real agent could make
equivalent_mutants.json   {"<mutant-id>": "why this mutant is equivalent / not observable"}; may be {}
llm_grades/<solution>.json   recorded output of llm_grade.py for each solution (committed)
```

### task.json

```json
{
  "id": "more-itertools-1345",
  "language": "python",                      // "python" | "typescript"
  "title": "numeric_range: equality and hash overflow above sys.maxsize",
  "upstream": {
    "repo": "more-itertools/more-itertools",
    "pr": 1345,
    "pr_url": "https://github.com/more-itertools/more-itertools/pull/1345",
    "base_sha": "<40 hex>",
    "merge_sha": "<40 hex>",
    "license": "MIT",
    "vendored_files": ["more_itertools/more.py", "..."]   // every file under upstream/, relative paths
  },
  "source_files": ["more_itertools/more.py"],           // files the fix is expected to touch
  "hidden_dest": "hidden_tests",                          // where hidden/ is copied inside the workspace
  "test_cmd": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "hidden_tests", "--junitxml={junit}"],
  "timeout_s": 120,
  "mutation": {"targets": [{"file": "more_itertools/more.py", "lines": [[2370, 2430]]}]}   // line ranges in the reference-patched file
}
```

`{junit}` is replaced with an absolute path. The harness parses JUnit XML for both pytest and vitest (`--reporter=junit --outputFile={junit}`). A test id is `<classname>::<name>` exactly as it appears in the JUnit XML. TypeScript tasks run vitest from the repo root's node_modules: workspaces live under `<repo>/.work/`, so Node resolution walks up to `<repo>/node_modules`.

### solutions.json

```json
{
  "reference": {"kind": "reference", "description": "upstream PR #1345 (merge f42dadd…)", "expect": "pass"},
  "alt-1":     {"kind": "alt", "description": "…", "author": "independent builder, no access to reference/hidden", "expect": "pass"},
  "flawed-1":  {"kind": "flawed", "description": "the mistake, in one line", "expect": "fail",
                "expect_failing": ["test id or check:<name>", "..."]}
}
```

A flawed solution matches its expectation only if its verdict is FAIL **and** every `expect_failing` item failed. So it must fail for the stated reason, not by accident. Reference and alt match only if their verdict is PASS.

## Grading (grade.py)

1. `ws = make_workspace(task, patch)`; copy `hidden/` to `ws/<hidden_dest>`. If the patch does not apply, or the toolchain or workspace fails, the verdict is ERROR (infrastructure), never PASS.
2. Run `test_cmd` in `ws` with `timeout_s`, writing JUnit XML. A timeout is FAIL (reason "timeout").
3. Run `bash checks.sh ws <pristine upstream>`; parse the PASS/FAIL lines.
4. Verdict is PASS iff all of these hold: exit 0; the JUnit file parses; total > 0; no failed, errored or skipped tests; every id in `expected_tests.json` is present; every check passes. Otherwise FAIL. No LLM is involved in the verdict.
5. Scope: every changed file must be inside the task's allowed paths. Relative imports resolve against the workspace package; external imports are checked in every allowed changed file.
Result dict: `{"verdict": "PASS"|"FAIL"|"ERROR", "tests": {"total", "passed", "failed": [ids]}, "checks": {"total", "passed", "failed": ["check:<name>"]}, "timed_out": bool}`. The reasons for each rule are in [docs/grading-decisions.md](docs/grading-decisions.md).

## Validation table (validate.py, deterministic)

One row per (task, solution), sorted by task id then reference, alt-*, flawed-*:
`| Task | Lang | Upstream PR | Solution | Kind | Hidden tests | Shell checks | LLM grader (recorded) | Verdict | Expected | Match |`
- Hidden tests and checks as `passed/total`. LLM grader column from `llm_grades/<solution>.json` ("PASS", "FAIL" or "—" if none recorded).
- Final line: `**N/N solutions match their expected verdict.**`
- No timings, paths or dates in the table. `make validate` exits non-zero if any row does not match, or if the README block differs (`readme.py --check`). `make readme` rewrites the block.
- README markers: `<!-- validation-table:start -->` / `<!-- validation-table:end -->` and `<!-- mutation-table:start -->` / `<!-- mutation-table:end -->`.

## Mutation testing (mutate.py)

Question it answers: do the hidden tests catch small mistakes in the code they are meant to protect?
- Apply reference.patch, then for each target line range generate first-order mutants, one at a time, with these text operators (skip comment lines and string literals):
  `==`↔`!=`, `===`↔`!==`, `<`→`<=`, `<=`→`<`, `>`→`>=`, `>=`→`>`, ` and `↔` or `, `&&`↔`||`, `not ` → remove, `True`↔`False`, `true`↔`false`, `+ 1`↔`- 1`, `return X` → `return None` (py) / `return undefined` (ts), negate a single-line `if`/`elif`/`while` condition (`not (…)` / `!(…)`), and statement deletion of a whole line that is a bare call or assignment (replace with `pass` in Python, keep indentation; empty in TS).
- Mutant id: `<file>:<line>:<operator>:<occurrence>` (stable).
- Run only the hidden tests (test_cmd) per mutant. Killed = verdict FAIL (including a timeout). A mutant whose grade is ERROR raises `MutationInfraError` and fails the gate; it never counts as killed.
- Gate: ≥5 mutants per task and ≥1 per target range. Every survivor must be in `equivalent_mutants.json` (`{mutant id: justification}`, ≥20 non-blank characters). A listed mutant that is now killed is a stale entry and fails the gate.
- Table: `| Task | Mutants | Killed | Equivalent (justified) | Unjustified survivors | Score |` + README check, same as validation.

## Agent runs (agent_runs.py, manual — never in CI)

For each task × agent × trial (5 trials):
1. **Boundary probe first** (`boundary_probe.py`). It plants canaries outside a probe workspace and asks each agent to read them, search for them, reach the network, and run pytest/vitest code that does the same. Each route is `allowed`, `denied` (explicit permission or sandbox evidence only) or `inconclusive` (fails the probe). Any HTTP status means the network is reachable. Results go to `runs/_boundary/` and are embedded in each attempt's meta.json.
2. **Workspace outside the repo:** `%TEMP%/pr2e-runs/<task>/<agent>/trial-<n>/attempt-<m>/`. It is a copy of `upstream/` + `TASK.md`, never `hidden/` or `solutions/`. TypeScript tasks get their own copy of `node_modules`. `git init` + one baseline commit, so the diff is clean. Environment: an allowlist of OS variables only (no tokens or API keys).
3. **Prompt:** "Read TASK.md in this directory and complete the task. Work only inside this directory. Run the existing tests if useful. When done, stop."
4. **Claude Code** (`--model sonnet`, env `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`): `--setting-sources project --strict-mcp-config --no-session-persistence --permission-mode dontAsk --allowedTools Read(./**) Edit(./**) Write(./**) Bash(python -m pytest:*) Bash(npx vitest:*) Bash(npx tsc:*) Bash(git diff:*) Bash(git status:*) Bash(git log:*) --disallowedTools WebFetch WebSearch Agent --output-format stream-json --verbose`. Bare Glob and Grep are not allowed: the probe measured them reading outside the workspace.
   **Codex:** `codex exec --ignore-user-config --ephemeral --skip-git-repo-check -s workspace-write -c windows.sandbox="elevated" -c sandbox_workspace_write.network_access=false -c project_doc_max_bytes=0 -m gpt-6.1-sol -c model_reasoning_effort="medium" --json -C <ws> <prompt>`.
   Both: wall-clock limit 20 min.
5. **Attempt identity:** the attempt dir is created exclusively and meta.json is written with `status: started` before launch. It is finalised as `complete` or `incomplete`, never deleted or overwritten. At most 2 attempts per trial; incomplete ones count toward the limit, and the batch exits non-zero.
6. **Evidence per attempt:** `meta.json` (agent, CLI version, requested and observed model, command line, start/end UTC, exit code, timed_out, embedded probe), `transcript.jsonl`, `stderr.txt`, `final.patch` (`git diff` against the baseline), `grade.json` (grade.py on final.patch in a fresh workspace, with `grader_digest`), `audit.json` (with `auditor_digest`), `llm_grade.json` (advisory) and `classification.json`.
7. **Audit (audit.py):** scans the transcript and changed files for reads outside the workspace, hidden-identifier leaks (contamination) and host-access patterns (`review_required`, cleared at confirmation with a reason).
8. **Classification:** `{"outcome": "pass"|"fail"|"contaminated", "failure_source": null|"agent"|"environment"|"grader", "evidence": "...", "classified_by": "..."}`. The auto-classifier proposes; the reviewer confirms every attempt with `agent_runs confirm`, quoting evidence.
9. **Staleness:** `agent_runs regrade` re-grades and re-audits any attempt whose patch, task, `grader_digest` or `auditor_digest` changed, appends to the history files and marks the classification stale.
10. **Report:** `report --write` writes `runs/SUMMARY.md`: per task × agent pass count k/5, the failure-source breakdown, the boundary statement and an "All attempts" section listing retries, contamination and incomplete attempts. `report --check` validates every attempt's evidence and bindings and fails on anything missing or stale.

**Boundary statement:** direct outside reads and network by the agents' own tools are denied for Claude (path-scoped file tools, Bash allowlist). Codex network is denied; Codex shell reads are detected, not prevented. Code the agent runs (its own tests) is detected by the audit (leak check plus static scan), NOT prevented. This is a documented limitation.

## Rules for every builder

- Never read or copy anything from another task's `hidden/` or `solutions/` unless your work order says so.
- Vendored upstream files must stay byte-identical to base_sha. Fetch them with `gh api` or raw.githubusercontent.com at the exact SHA.
- No secrets, no personal data, no absolute paths from this machine in committed files.
- Do not commit. Do not push.
