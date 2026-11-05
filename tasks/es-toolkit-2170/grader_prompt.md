You are reviewing a patch written by a coding agent for the task below. Judge only what the diff does. Do not assume anything that is not visible in the diff.

## Task

{{TASK}}

## Diff

```diff
{{DIFF}}
```

## Rubric

Check each point against the diff:

1. Timer cleanup on every settle path. The timeout timer that `withTimeout` starts must be cleared when `run()` resolves, when `run()` rejects, and when the call times out. A fix that clears it only on success fails this point.
2. Listener cleanup. Any abort listener that `withTimeout` adds to the caller-supplied `signal` must be removed after the call settles, on success, failure and timeout, and a signal that is already aborted must be handled. A listener that stays attached fails this point.
3. TimeoutError behaviour unchanged. If `run()` is slower than `ms`, the promise must still reject with `TimeoutError`. Removing, weakening or bypassing the timeout race fails this point. The rejection reason and the result of `run()` must pass through unchanged (no wrapping, no swallowing).
4. Existing signal semantics unchanged. Aborting the signal still lifts the time limit; it does not reject the call.
5. Signature unchanged. The exported `withTimeout` keeps its parameters and return type.
6. Minimal scope. The change touches only the source under `src/promise/` (and optionally spec files). No unrelated refactors, no new dependencies, no edits to other modules.

Scoring:
- correctness: 0 = does not fix the problem or breaks behaviour, 1 = fixes only some paths, 2 = fixes all paths with a minor concern, 3 = fully correct (points 1 to 5).
- scope: 0 = large unrelated changes, 1 = noticeable extra changes, 2 = small extras, 3 = minimal and focused (point 6).
- verdict is "PASS" only if correctness is 3 and scope is at least 2. Otherwise "FAIL".

Reply with JSON only, no other text, in exactly this shape:
{"verdict": "PASS" | "FAIL", "correctness": 0-3, "scope": 0-3, "reasons": "short explanation citing the diff"}
