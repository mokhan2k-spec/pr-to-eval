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

1. `ws = make_workspace(task, patch)`; copy `hidden/` to `ws/<hidden_dest>`.
2. Run `test_cmd` in `ws` with `timeout_s`, `PYTHONPATH=ws`. A timeout counts as all tests failed (reason "timeout").
3. Run `bash checks.sh ws <pristine upstream>`; parse the PASS/FAIL lines.
4. Verdict is PASS iff ≥1 test ran, all tests passed and all checks passed. No LLM is involved in the verdict.
Result dict: `{"verdict", "tests": {"total", "passed", "failed": [ids]}, "checks": {"total", "passed", "failed": ["check:<name>"]}, "timed_out": bool}`.

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
  `==`↔`!=`, `===`↔`!==`, `<`→`<=`, `<=`→`<`, `>`→`>=`, `>=`→`>`, ` and `↔` or `, `&&`↔`||`, `not ` → remove, `True`↔`False`, `true`↔`false`, `+ 1`↔`- 1`, `return X` → `return None` (py) / `return undefined` (ts), and statement deletion of a whole line that is a bare call or assignment (replace with `pass` in Python, keep indentation; empty in TS).
- Mutant id: `<file>:<line>:<operator>:<occurrence>` (stable).
- Run only the hidden tests (test_cmd) per mutant; killed = any test failed or timeout or crash.
- Fail if any surviving mutant is not in `equivalent_mutants.json`, or if a listed one is now killed (stale entry).
- Table: `| Task | Mutants | Killed | Equivalent (justified) | Unjustified survivors | Score |` + README check, same as validation.

## Agent runs (agent_runs.py, manual — never in CI)

For each task × agent × n (default 5):
1. Workspace OUTSIDE the repo: `%TEMP%/pr-to-eval-runs/<task>-<agent>-<n>/` = copy of `upstream/` + `TASK.md`; TypeScript tasks also get `package.json` + `package-lock.json` and `npm ci`. `git init` + one baseline commit, so the diff is clean.
2. Prompt: "Read TASK.md in this directory and complete the task. Work only inside this directory. Run the existing tests if useful. When done, stop."
3. Claude Code: env `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`; `claude -p <prompt> --model sonnet --setting-sources project --strict-mcp-config --no-session-persistence --permission-mode bypassPermissions --disallowedTools WebFetch WebSearch Agent --output-format stream-json --verbose`.
   Codex: `codex exec --ignore-user-config --ephemeral --skip-git-repo-check -s workspace-write -m gpt-6.1-sol -c model_reasoning_effort="medium" --json -C <ws> <prompt>`.
   Both: cwd = workspace, wall-clock limit 20 min.
4. Store `runs/<task>/<agent>-<n>/`: `meta.json` (agent, cli version, model, command line with the prompt, start/end UTC, exit code, timed_out), `transcript.jsonl` (stdout), `stderr.txt`, `final.patch` (`git diff` against the baseline, excluding node_modules), `grade.json` (grade.py on final.patch applied to a fresh workspace), `classification.json`.
5. classification.json: `{"outcome": "pass"|"fail", "failure_source": null|"agent"|"environment"|"grader", "evidence": "...", "classified_by": "..."}`. Auto-classifier proposes: no patch + non-zero exit / CLI error → environment; patch applies but tests fail → agent; patch fails to apply → environment. A human (or the main reviewer) confirms every failure and may change it to grader, with the evidence quoted.
6. `runs/SUMMARY.md`: per task × agent pass count k/5 and the failure-source breakdown.

## Rules for every builder

- Never read or copy anything from another task's `hidden/` or `solutions/` unless your work order says so.
- Vendored upstream files must stay byte-identical to base_sha. Fetch them with `gh api` or raw.githubusercontent.com at the exact SHA.
- No secrets, no personal data, no absolute paths from this machine in committed files.
- Do not commit. Do not push.
