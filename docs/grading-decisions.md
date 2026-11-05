# Grading decisions

This file records each rule the graders follow, why it exists, and what changed it. Newest entries are at the bottom of each section. Review ids (R01–R17) are the findings from the independent Codex review of the week-2 plan.

## Verdict rules (grade.py)

| # | Decision | Why |
|---|---|---|
| G1 | PASS requires all of: exit 0, a JUnit file that parses, zero failed, errored or skipped tests, total > 0, every id in `expected_tests.json` present, and every shell check passing. | The first rule was "all tests passed", which a run with zero collected tests, or with tests silently skipped, could satisfy (R01). |
| G2 | A timeout is FAIL, never PASS or ERROR. | A solution that hangs is wrong for a user. Infrastructure failure is a separate class. |
| G3 | ERROR is reserved for infrastructure: the patch would not apply, the toolchain is missing, or the workspace could not be built. It is never a pass. | This keeps "the agent's code is wrong" apart from "our harness broke", which the failure classification depends on (R01). |
| G4 | No LLM takes part in the verdict. The LLM grade is recorded beside it as advisory, and its agreement with the deterministic verdict is reported. | An LLM verdict cannot be reproduced from a fresh clone; the tests can. |
| G5 | A flawed solution counts as matched only if it FAILs and every test listed in its `expect_failing` failed. | A flawed fix must fail for its stated reason, not by accident (for example, a crash in an unrelated test). |
| G6 | Each solution may change only files in the task's allowed scope. Relative imports resolve against the workspace package, and external imports are checked in every allowed changed file. | Scope stops a solution from editing tests to make them pass (R10). Helper modules inside the package are legitimate (R16). |
| G7 | more-itertools-1345's scope was widened to `more_itertools/*` and `tests/*`. | The upstream fix adds tests in `tests/test_more.py`. Hidden tests are copied in after the patch, so a solution editing visible tests cannot affect the verdict. |

## Corpus and provenance (validate.py, verify_upstream.py)

| # | Decision | Why |
|---|---|---|
| P1 | The corpus gate requires ≥3 flawed solutions and ≥1 independent alternative per task (`corpus.json`). Alternatives are written by an agent that sees only the task prompt and visible files. | An alt that saw the reference proves nothing about the grader (R07). |
| P2 | `verify_upstream` requires `base_sha` to be a parent of `merge_sha`, or a `base_note` explaining the gap. | boltons #458 and es-toolkit #2170 were squash-merged one commit past their PR base. The tasks are built on the PR base, so the agent sees the tree the PR author saw (R03). |
| P3 | Reference-patched files must byte-match `merge_sha`, or carry a `reference_deviations` entry with a justification. Stale entries are flagged. | boltons `urlutils.py` differs only at lines 967 and 969. That change is #455 (d45cc414e3), merged between the PR base and the squash merge and unrelated to #458. The diff was checked line by line. |

## Mutation gate (mutate.py)

| # | Decision | Why |
|---|---|---|
| M1 | Each task needs ≥5 mutants, and each target range needs ≥1. Any survivor must be listed in `equivalent_mutants.json` with a justification of ≥20 non-blank characters. A listed mutant that is now killed is a stale entry and fails the gate. | A small mutant pool proves little, and an unexplained survivor is a test gap (R08). |
| M2 | A mutant that ERRORs raises `MutationInfraError` and fails the gate. It never counts as killed. | Counting a broken build as "killed" would inflate the score. |
| M3 | Added the `negate-cond` operator, which wraps a single-line `if`/`elif`/`while` condition as `not (…)` in Python or `!(…)` in TypeScript. | es-toolkit-2170's fix range already covers the whole `withTimeout` function and yielded only 4 mutants. Widening the range was impossible, so the operator set grew instead. Unit-tested in `tests/test_harness.py`. |
| M4 | **Killed:** `more_itertools/more.py:2374:return-none:1`. The hidden test now asserts `r.__eq__(other) is False` for non-range operands. | `numeric_range.__eq__` returning `None` instead of `False` went undetected: `assertFalse(r == other)` accepts any falsy value. The reference still passes 42/42, and all four flawed solutions still fail for their stated reasons. |
| M5 | **Equivalent:** `more_itertools/more.py:2442:le-lt:1`. | At distance 0 the else branch also returns 0 (q = r = 0, and neither settling loop runs). |
| M6 | **Equivalent:** `more_itertools/more.py:2446:ne-eq:1` and `more_itertools/more.py:2451:minus1-plus1:1`. | They only move the initial length estimate. The two settling loops always converge to the exact count; only the number of iterations changes. |
| M7 | **Equivalent:** `src/promise/withTimeout.ts:55:true-false:1` (`once: true` → `once: false`). | An AbortSignal dispatches `abort` at most once, and the listener is removed with the controller's signal anyway. |

## Agent runs (agent_runs.py, audit.py)

| # | Decision | Why |
|---|---|---|
| A1 | Agent workspaces live outside the repo and contain no `hidden/` or `solutions/` files. Claude's Read tool is path-scoped to the workspace. | The 2026-11-05 boundary probe showed that an unscoped Read, and Codex `cat`, could read a file outside the workspace (R02). |
| A2 | Isolation of code the agent itself runs (for example, inside its own tests) is detected by heuristics, not prevented. The probe records those routes, and the README states the limit. | Process isolation on this host would need Windows security setting changes, which are out of bounds (R11). |
| A3 | Any HTTP status in the probe means the network is reachable. "Denied" needs explicit permission or sandbox evidence; anything else is inconclusive and fails the probe. | Avoids reading an error page as a block (R12). |
| A4 | `grade.json` binds a digest of the grader, and `audit.json` binds a digest of the auditor. A stale digest invalidates the classification and fails `report --check`. | A grade made by an older grader must not stand silently (R13). |
| A5 | Every attempt is persisted before launch and never auto-deleted. Incomplete attempts count toward the retry limit, and SUMMARY lists every attempt, including retries. | Stops quiet re-running until a run looks good (R14, R15). |
| A6 | No launch without a passing boundary probe bound to the current CLI version, run policy (argv with the workspace as a placeholder, env allowlist, read scope, model, effort), probe code and platform. `report --check` re-checks the binding on every attempt. | A probe of an older CLI or command line says nothing about the runs (R18). The digest is pinned to the run's platform so a Linux CI checkout can verify a Windows run. |
| A7 | Each probe route is judged only on its own tagged runner lines. Shared denial evidence counts only when the whole runner invocation was refused. | One route's denial must not mark another route as denied (R19). |
| A8 | The hidden-identifier check also scans tool results (what the agent saw), not only what it wrote. | An agent can read a leaked identifier without ever repeating it (R20). |
| A9 | `started` blocks a retry. `incomplete` allows one only after `confirm --interrupted`, which inventories every salvaged file by sha256. Started, incomplete and interrupted attempts are frozen: regrade and re-audit never rewrite them, even with `--all`. | A crashed attempt cannot be hidden by a retry, and its inventory stays valid (R21, R24). |
| A10 | The probe binding records the read scope it measured. A `--scope absolute` probe cannot certify the default relative policy, and a partial re-run cannot mix scopes. | A probe certifies only the policy it actually measured (R25). |
| A11 | Privacy allowlist entries are scoped to one rule, one path glob and the matched value. Whole-line exemption is an explicit, anchored `exact_line` mode, used only for the owner's own no-reply git author line. | A line-wide example.com exemption had been hiding another address on the same line (R22). |
| A12 | Alt solutions go through `agent_runs alt`, with the same probe gate and command as the trials, into `runs/_alts/<label>/`. The only difference is an optional constraint appended to the prompt and recorded in `meta.prompt` (for example, forbidding upstream's approach). | An alt is only independent evidence if it was made under the same boundary as the trials, by an agent that never saw the reference. |
| A13 | An alt enters the corpus only if it passes the grader and its audit is clean, or its audit hits were reviewed and confirmed false positives (`confirm --audit-false-positive` with written evidence). Contaminated alts stay in `runs/_alts/` as evidence and are never deleted. Each task keeps at least one alt whose description says how it differs structurally from upstream. When an unconstrained alt converged on upstream, a second alt was commissioned with a constraint forbidding the upstream approach (alt-3 for all three tasks). | An alt that copies upstream's design shows that the tests accept upstream again, nothing more. An alt from a contaminated run is not independent. |
| A14 | The Codex alt-2 runs (all three tasks) are excluded as contaminated. Codex 0.160.1 loads the host's global `$CODEX_HOME/AGENTS.md` even with `--ignore-user-config` and `project_doc_max_bytes=0`. Following it, the agent read `~/.codex/RTK.md` (outside the workspace), and in some runs delegated to a local model. No per-run switch disables this; the fix is pending the owner's decision. | The audit caught the leak. The exclusion is recorded rather than silently re-run. |
| A15 | The 15 Codex trials were run on this host as they are and confirmed with `confirm --host-configured`. Each one keeps its verdict but is labelled "host-configured: personal ~/.codex/AGENTS.md loaded, NOT independent". Every such trial is kept out of the headline figure, which counts independent trials only. `--host-configured` is refused unless the attempt is a Codex attempt, its audit shows a read under `~/.codex`, and every other audit hit was reviewed as a false positive. The record stores the `~/.codex` reads and the `qwen` helper calls the audit saw, and `report --check` recomputes them from `audit.json`. The clean fix is a rerun with an empty `CODEX_HOME`, which needs the owner's sign-in and is handed back. The harness strips every variable outside `ENV_ALLOW`, so that rerun first needs `CODEX_HOME` passed through for Codex only. That changes the Codex policy digest, so `boundary_probe --agent codex` must be rerun before the trials. The Claude digest is unchanged. | The leak is the host's personal config, not task data, so the verdicts are still informative. But the agent had help the Claude runs did not have: a local model sometimes drafted code. Calling these runs independent would be false. Naming the contamination is better than hiding it or dropping the runs. |
