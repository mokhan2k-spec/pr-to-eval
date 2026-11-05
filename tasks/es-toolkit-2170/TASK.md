# withTimeout leaves its timer and abort listener behind after the operation settles

`withTimeout(run, ms, { signal? })` in `src/promise/withTimeout.ts` runs `run()` and rejects with a `TimeoutError` if it takes longer than `ms` milliseconds.

## Problem

Once the operation has finished, `withTimeout` does not clean up after itself:

- After `run()` resolves or rejects, the timeout timer is still pending. It keeps the event loop alive until it fires, and it is still counted as an active timer.
- When the caller passes a `signal`, the abort listener that `withTimeout` attaches to that signal is still attached after the call has settled. A signal reused across many calls collects one extra listener per call.

This happens on every path: when `run()` resolves, when `run()` rejects, and when the call times out.

## Required behaviour

- After `withTimeout(run, ms)` settles, whether `run()` resolved or rejected, no timer started by `withTimeout` is still pending.
- After it settles, no abort listener added by `withTimeout` is left on the caller's `signal`. This applies on success, on failure and on timeout.
- If `run()` takes longer than `ms`, the returned promise still rejects with `TimeoutError` (message `The operation was timed out`), as it does today.
- The result value of `run()` and its rejection reason still pass through unchanged.
- The existing `signal` semantics stay as they are: aborting the signal lifts the time limit and `run()`'s promise is awaited without a deadline. This also holds for a signal that is already aborted when the call starts.
- The public signature of `withTimeout` does not change.

## Notes

- The existing tests are in `src/promise/withTimeout.spec.ts`. You can run them with `npx vitest run src/promise/withTimeout.spec.ts` if vitest is available in your environment.
- Keep the change small and limited to the source under `src/promise/`.
