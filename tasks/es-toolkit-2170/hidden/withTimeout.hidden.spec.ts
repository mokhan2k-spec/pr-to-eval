import { getEventListeners } from 'node:events';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TimeoutError } from '../src/error/TimeoutError.ts';
import { delay } from '../src/promise/delay.ts';
import { withTimeout } from '../src/promise/withTimeout.ts';

const abortListenerCount = (signal: AbortSignal) => getEventListeners(signal, 'abort').length;

// A promise that never settles, so only the timeout can end the race.
const never = () => new Promise<never>(() => {});

describe('withTimeout cleanup (hidden)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it('leaves no pending timers after the operation resolves', async () => {
    await expect(withTimeout(async () => 'foo', 100)).resolves.toBe('foo');

    expect(vi.getTimerCount()).toBe(0);
  });

  it('leaves no pending timers after the operation resolves later than started', async () => {
    const promise = withTimeout(async () => {
      await delay(30);
      return 'late';
    }, 100);

    await vi.advanceTimersByTimeAsync(30);

    await expect(promise).resolves.toBe('late');
    expect(vi.getTimerCount()).toBe(0);
  });

  it('leaves no pending timers after the operation rejects', async () => {
    const reason = new Error('boom');

    await expect(withTimeout(() => Promise.reject(reason), 100)).rejects.toBe(reason);

    expect(vi.getTimerCount()).toBe(0);
  });

  it('rejects with TimeoutError when the operation is slower than the limit, leaving no timers', async () => {
    const promise = withTimeout(never, 50);
    const settled = promise.then(
      () => null,
      (error: unknown) => error
    );

    await vi.advanceTimersByTimeAsync(50);

    const error = await settled;
    expect(error).toBeInstanceOf(TimeoutError);
    expect((error as Error).message).toBe('The operation was timed out');
    expect(vi.getTimerCount()).toBe(0);
  });

  it('passes the result value through unchanged', async () => {
    const value = { nested: [1, 2, 3] };

    await expect(withTimeout(async () => value, 100)).resolves.toBe(value);
  });

  it('passes the rejection reason through unchanged', async () => {
    const reason = new TypeError('bad input');

    const error = await withTimeout(() => Promise.reject(reason), 100).catch((e: unknown) => e);

    expect(error).toBe(reason);
  });

  it('does not time out an operation that finishes before the limit', async () => {
    const promise = withTimeout(async () => {
      await delay(40);
      return 'in time';
    }, 50);

    await vi.advanceTimersByTimeAsync(40);
    await expect(promise).resolves.toBe('in time');

    // The original deadline passing later must not change anything.
    await vi.advanceTimersByTimeAsync(1000);
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('withTimeout signal cleanup (hidden)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it('removes its abort listener from the signal after the operation resolves', async () => {
    const controller = new AbortController();

    await expect(withTimeout(async () => 'foo', 100, { signal: controller.signal })).resolves.toBe('foo');

    expect(abortListenerCount(controller.signal)).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('removes its abort listener from the signal after the operation rejects', async () => {
    const controller = new AbortController();
    const reason = new Error('boom');

    await expect(withTimeout(() => Promise.reject(reason), 100, { signal: controller.signal })).rejects.toBe(reason);

    expect(abortListenerCount(controller.signal)).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('removes its abort listener from the signal after timing out', async () => {
    const controller = new AbortController();
    const settled = withTimeout(never, 50, { signal: controller.signal }).then(
      () => null,
      (error: unknown) => error
    );

    await vi.advanceTimersByTimeAsync(50);

    expect(await settled).toBeInstanceOf(TimeoutError);
    expect(abortListenerCount(controller.signal)).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('still lifts the time limit when the signal is aborted before completion', async () => {
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 25);

    const promise = withTimeout(
      async () => {
        await delay(100);
        return 'foo';
      },
      50,
      { signal: controller.signal }
    );

    await vi.advanceTimersByTimeAsync(100);

    await expect(promise).resolves.toBe('foo');
    expect(abortListenerCount(controller.signal)).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('lifts the time limit when the signal is already aborted', async () => {
    const controller = new AbortController();
    controller.abort();

    const promise = withTimeout(
      async () => {
        await delay(100);
        return 'foo';
      },
      50,
      { signal: controller.signal }
    );

    await vi.advanceTimersByTimeAsync(100);

    await expect(promise).resolves.toBe('foo');
    expect(vi.getTimerCount()).toBe(0);
  });

  it('does not accumulate listeners on a signal reused across calls', async () => {
    const controller = new AbortController();

    for (let i = 0; i < 5; i++) {
      await withTimeout(async () => i, 100, { signal: controller.signal });
    }

    expect(abortListenerCount(controller.signal)).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });
});
