import { describe, expect, it, vi } from "vitest";
import { createEditTailScheduler } from "./editTail";

describe("createEditTailScheduler", () => {
  it("an edit alone (constructing the scheduler, never calling run()) triggers no fetch, at any elapsed time (replaces the old debounce test)", () => {
    vi.useFakeTimers();
    try {
      const runTail = vi.fn().mockResolvedValue(undefined);
      createEditTailScheduler(runTail);
      vi.advanceTimersByTime(10_000);
      expect(runTail).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("run() starts a tail immediately, with no window to wait out", async () => {
    const runTail = vi.fn().mockResolvedValue(undefined);
    const scheduler = createEditTailScheduler(runTail);

    scheduler.run();
    expect(runTail).toHaveBeenCalledTimes(1);
  });

  it("a press with nothing pending still runs the tail (the deferral-recovery path)", () => {
    // The scheduler itself has no notion of "pending edits" -- that decision
    // (edit batch vs. plain re-resolve) lives in the caller's runTail
    // closure. From the scheduler's side, run() unconditionally runs.
    const runTail = vi.fn().mockResolvedValue(undefined);
    const scheduler = createEditTailScheduler(runTail);

    scheduler.run();
    expect(runTail).toHaveBeenCalledTimes(1);
  });

  it("a second run() while the first is in flight aborts it and starts exactly one more", () => {
    const signals: AbortSignal[] = [];
    const runTail = vi.fn((signal: AbortSignal) => {
      signals.push(signal);
      return new Promise<void>(() => {
        /* never resolves in this test -- only the abort matters */
      });
    });
    const scheduler = createEditTailScheduler(runTail);

    scheduler.run();
    expect(runTail).toHaveBeenCalledTimes(1);
    expect(signals[0].aborted).toBe(false);

    // A second press while the first is still running: supersede, don't queue.
    scheduler.run();
    expect(signals[0].aborted).toBe(true);
    expect(runTail).toHaveBeenCalledTimes(2);
    expect(signals[1].aborted).toBe(false);
  });

  it("cancel() with nothing in flight is a no-op", () => {
    const runTail = vi.fn().mockResolvedValue(undefined);
    const scheduler = createEditTailScheduler(runTail);

    expect(() => scheduler.cancel()).not.toThrow();
    expect(runTail).not.toHaveBeenCalled();
  });

  it("cancel() aborts an in-flight run", () => {
    let sawAbort = false;
    const runTail = vi.fn((signal: AbortSignal) => {
      signal.addEventListener("abort", () => {
        sawAbort = true;
      });
      return new Promise<void>(() => {});
    });
    const scheduler = createEditTailScheduler(runTail);

    scheduler.run();
    expect(runTail).toHaveBeenCalledTimes(1);

    scheduler.cancel();
    expect(sawAbort).toBe(true);
  });

  it("a run that resolves cleanly leaves no in-flight controller for a later cancel() to touch", async () => {
    const runTail = vi.fn().mockResolvedValue(undefined);
    const scheduler = createEditTailScheduler(runTail);

    scheduler.run();
    // Let the resolved promise's `.finally` clear the controller.
    await Promise.resolve();
    await Promise.resolve();

    expect(() => scheduler.cancel()).not.toThrow();
  });
});
