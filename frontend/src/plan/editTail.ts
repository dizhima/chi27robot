/**
 * Pure run-now/cancel scheduler for the manual-edit tail
 * (compound_turn_integration_spec.md §5 item 17, revised / design §3 revised).
 *
 * Originally an 800ms debounce that auto-fired after a burst of edits. That
 * was removed: a tail run is several full verification compiles (integration
 * spec §10), so auto-running per edit meant ten-plus seconds per drag, and an
 * edit made mid-run aborted the very run it was waiting for. There is no
 * "right" batching window to guess, because the point at which a user
 * considers their edits done is a judgment call only they can make — so the
 * timing is now entirely theirs: nothing runs until the sync button (or
 * equivalent explicit trigger) calls `run()`.
 *
 * What's left to own here is exactly the single-in-flight invariant: at most
 * one run's `AbortController` is ever live. `run()` always supersedes
 * whatever was in flight, which is what turns "press sync while a run is
 * still going" into "abort that one, start fresh" rather than letting two
 * runs race (the backend's `base_revision` guard would only clean up the
 * RESULT of that race, not prevent the wasted work).
 */

export type EditTailScheduler = {
  /** Abort any in-flight run (superseding it) and start a fresh one
   *  immediately — no debounce window, no queuing. */
  run: () => void;
  /** Abort any in-flight run, with no replacement started. Used when a
   *  visible user action (chat turn, revert) closes the current edit episode
   *  and the in-flight run's reply would just be discarded anyway. */
  cancel: () => void;
};

export function createEditTailScheduler(
  runTail: (signal: AbortSignal) => Promise<void>,
): EditTailScheduler {
  let controller: AbortController | null = null;

  const cancel = () => {
    if (controller !== null) {
      controller.abort();
      controller = null;
    }
  };

  const run = () => {
    // Superseding an in-flight run (rather than letting a second one start
    // alongside it) is what keeps "at most one run's AbortController is ever
    // live" true, and is exactly what a second sync press while one is
    // already running needs: cancel the stale one, run the current batch.
    cancel();
    const current = new AbortController();
    controller = current;
    void runTail(current.signal).finally(() => {
      if (controller === current) controller = null;
    });
  };

  return { run, cancel };
}
