You are a strict code reviewer. You are grading a proposed change to the `more-itertools` library.

## The task given to the author

{{TASK}}

## The proposed change (unified diff)

{{DIFF}}

## Rubric

Judge the diff only against the task text above. Do not assume tests exist beyond what the diff shows. Score each criterion:

1. Correctness for lengths above `sys.maxsize`: `==`, `!=` and `hash()` on `numeric_range` must return correct answers (not raise, not guess) for ranges with more than `sys.maxsize` values, ascending and descending. Ranges with equal content but different arguments (for example a different `stop`) must compare equal.
2. Eq/hash consistency: equal ranges must hash equal, and the hash must stay consistent with the existing hashing scheme (mirroring the built-in `range`). Swallowing `OverflowError` and returning a constant or a partial hash is incorrect.
3. No public API change: `len()` of a range longer than `sys.maxsize` must still raise `OverflowError`, `len()` of normal ranges must be unchanged, and no public names or signatures may change.
4. Minimal scope: the change should touch only what the bug requires. Unrelated refactors, new public features or edits to unrelated functions lower the scope score.

Scoring:
- `correctness`: 0 (wrong or crashes), 1 (partly right, a rubric item 1-3 clearly violated), 2 (right with a minor concern), 3 (fully right).
- `scope`: 0 (large unrelated changes), 1 (noticeable extra changes), 2 (small extras), 3 (minimal).
- `verdict`: "PASS" only if correctness is at least 2 and scope is at least 2 and no rubric item 1-3 is violated. Otherwise "FAIL".

Respond with JSON only, no prose and no code fences, exactly in this shape:
{"verdict": "PASS" or "FAIL", "correctness": 0-3, "scope": 0-3, "reasons": "one or two sentences"}
