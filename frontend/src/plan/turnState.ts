/**
 * Pure state-transition core for adopting a compound-turn `turn_result`
 * (compound_turn_integration_spec.md D1/§5 items 15+18). No React, no fetch —
 * ScenePage calls this and applies the result to its own state, but the
 * decision logic (stale-artifact guard, what a history node captures, what
 * revert restores) is unit-testable on its own.
 *
 * D1's corollary: the frontend never holds "the authored plan" — decompose()
 * is backend-only and re-runs from `semanticActions` every turn. So the
 * frontend's committable state reduces to exactly: `semanticActions` (input),
 * `plan` + `compile` (display, ScenePage's `livePlan`/`liveCompile`), and one
 * history node for one-click revert. `draftPlan` (ScenePage's manual-edit
 * preview) is deliberately NOT part of this — it is local UI state, never a
 * committed state (see the comment above ScenePage's `draftPlan`).
 */
import type { AugmentedAction } from "./authorPlan";
import type { AuthoredPlan, CompileResponse } from "./planTypes";
import type { PlanEditDelta } from "./planEdits";

export type LiveState = {
  /** Monotone counter (integration spec §3), bumped on every commit and every
   *  local edit. A `turn_result` whose echoed `base_revision` doesn't match
   *  this is stale — it was computed against an input that no longer holds —
   *  and MUST be discarded rather than applied. */
  baseRevision: number;
  semanticActions: AugmentedAction[];
  plan: AuthoredPlan | null;
  compile: CompileResponse | null;
  /** D2b (revises S3/D2): the ordered manual-edit deltas made SINCE the last
   *  commit and not yet applied anywhere — a pending batch, not a replay
   *  ledger. D2's original design replayed the whole accumulated history of
   *  edits onto a fresh `decompose(actions)` every turn, which is what made
   *  editing a resolver artifact (a `go_to_rest` leg) both a no-op AND a
   *  reason prior repairs got discarded (see conversation.py's
   *  `stream_compound_turn` docstring for the full diagnosis). Under D2b a
   *  delta is baked into the plan the moment it is applied, so there is
   *  nothing left to replay next turn — `edits` empties out on every
   *  successful commit (see `commitTurnResult`'s `sentEdits`) rather than
   *  growing forever. Snapshotted into a HistoryNode so revert restores
   *  whatever batch was still pending right before the reverted turn, not
   *  just the plan itself. */
  edits: PlanEditDelta[];
};

/**
 * One committed checkpoint, kept for exactly one step of undo (item 18,
 * depth N = 1 for v1). A FULL snapshot, not just plan/compile: restoring only
 * those two would leave the next turn running on the newer `semanticActions`,
 * silently re-diverging from the plan the user just reverted to (the
 * "split-brain" item 18 warns about). `edits` is always `[]` in S2 — there is
 * no `PlanEditDelta` yet (that's S3) — but the field exists now so a later
 * slice does not have to reshape this node.
 */
export type HistoryNode = {
  revision: number;
  plan: AuthoredPlan;
  compile: CompileResponse;
  semanticActions: AugmentedAction[];
  edits: PlanEditDelta[];
};

/** The subset of a `turn_result` TurnOutcome this module needs, kept narrow
 *  so it has no dependency on the stream client's types. */
export type TurnResultOutcome = {
  plan: AuthoredPlan | null;
  compile?: CompileResponse;
  actions?: AugmentedAction[];
  baseRevision?: number;
};

/** What triggered this commit (item 18's episode-coalescing rule):
 *  - "chat"/"sync": a visible user action (send a message, press sync with
 *    nothing pending — a plain re-resolve) that always starts (and, via the
 *    returned `episode: null`, CLOSES) its own history node.
 *  - "edit": pressing sync with a pending edit batch (item 17, revised). Since
 *    edits no longer auto-run a tail, "edit" no longer means "one of several
 *    auto-fired commits within a debounce window" — a batch is applied only
 *    by an explicit press, so in practice one "edit" commit already IS the
 *    whole episode. The coalescing machinery stays anyway: a second press
 *    superseding an in-flight run (editTail.ts) must still land as ONE node,
 *    not two, which is exactly what reusing `openEpisode` below guards. */
export type CommitTrigger = "chat" | "sync" | "edit";

export type CommitOptions = {
  trigger?: CommitTrigger;
  /** D2b: the pending deltas that were actually SENT to the backend with
   *  THIS turn (the caller's `pendingEditsRef.current` read synchronously at
   *  request-issue time, before the `await`). Once a `turn_result` commits,
   *  every one of them has been consumed one way or another — baked into the
   *  returned plan, or dropped with a visible warning — so none of them stay
   *  "pending" any more. `nextState.edits` is computed as `state.edits`
   *  (read fresh, i.e. AFTER the await, so it reflects any edit the user made
   *  mid-flight) minus exactly this set, by reference identity — a delta
   *  object is only ever created once (`editDraft`'s `[...prev, delta]`) and
   *  never mutated, so `.includes` is a safe identity check. Omitted (every
   *  pre-D2b caller and every test that doesn't care about edits bookkeeping)
   *  leaves `state.edits` untouched. */
  sentEdits?: PlanEditDelta[];
  /** The node captured when the CURRENT open edit episode began, or `null`
   *  if none is open. Only consulted when `trigger === "edit"` — reusing it
   *  verbatim (never recomputing from `state`) is what keeps consecutive
   *  edit-tail commits from overwriting the node the episode started with
   *  (design §5 item 18: "no idle timer, only these two visible triggers"). */
  openEpisode?: HistoryNode | null;
};

export type CommitResult =
  | {
      applied: true;
      state: LiveState;
      historyNode: HistoryNode | null;
      /** The open-episode value the caller should retain for its NEXT
       *  commit's `openEpisode`: the just-produced node for an "edit"
       *  commit (continuing/opening the episode), `null` for "chat"/"sync"
       *  (closing whatever episode was open — item 18's episode boundary). */
      episode: HistoryNode | null;
    }
  // Stale base_revision: the artifact was computed against a state that has
  // since moved on. `state`/`historyNode` are not returned -- the caller
  // makes no state change at all (discarded, not applied).
  | { applied: false };

/**
 * Apply one committed `turn_result` to the current live state. Per D5/§7 a
 * turn_result commits automatically -- there is no draft/review gate to pass
 * through (item 15 deletes it) -- so this is the entire "terminal artifact
 * application" for the compound path, and it runs unconditionally: whether
 * or not the resolver converged (item 18 — "every committed artifact gets a
 * node, converged or not"; convergence is a `report` detail this function
 * does not need to inspect) and whether or not the author stage produced any
 * actions (a no-actions/no-plan turn still bumps the revision and keeps the
 * previous plan, matching stream_compound_turn's own no-actions case).
 */
export function commitTurnResult(
  state: LiveState,
  outcome: TurnResultOutcome,
  options: CommitOptions = {},
): CommitResult {
  if (outcome.baseRevision !== undefined && outcome.baseRevision !== state.baseRevision) {
    return { applied: false };
  }
  const trigger = options.trigger ?? "chat";
  // D2b: clear exactly the batch this turn consumed, keeping anything pushed
  // to `state.edits` after the request was sent (a concurrent Gantt edit
  // made while this turn was in flight) — see `sentEdits`'s doc comment.
  const nextEdits = options.sentEdits
    ? state.edits.filter((edit) => !options.sentEdits!.includes(edit))
    : state.edits;

  // A prior committed plan is what the user could want to undo BACK to; the
  // very first turn in a session has none, so there is nothing to snapshot.
  const freshNode: HistoryNode | null =
    state.plan && state.compile
      ? {
          revision: state.baseRevision,
          plan: state.plan,
          compile: state.compile,
          semanticActions: state.semanticActions,
          edits: state.edits,
        }
      : null;

  // Episode coalescing (item 18): an "edit" commit inside an already-open
  // episode reuses that episode's node verbatim rather than re-snapshotting
  // `state` (which has moved since the episode began). This still matters
  // post-revision: a sync press that supersedes an in-flight sync press
  // (editTail.ts's `run()`) must land as the SAME node the superseded one
  // would have, or the second press would silently overwrite what the user
  // could revert to before the episode began -- one node short of what "one
  // click back" is supposed to mean. A "chat"/"sync" commit always takes the
  // fresh snapshot and, by returning `episode: null`, closes any open
  // edit episode -- there is no idle timer, only these two visible triggers
  // (a chat turn, or a sync press with nothing pending).
  const historyNode = trigger === "edit" && options.openEpisode ? options.openEpisode : freshNode;
  const episode = trigger === "edit" ? historyNode : null;

  const nextActions = outcome.actions ?? state.semanticActions;
  const hasNewPlan = outcome.plan !== null && outcome.compile !== undefined;
  const nextState: LiveState = {
    baseRevision: state.baseRevision + 1,
    semanticActions: nextActions,
    plan: hasNewPlan ? outcome.plan : state.plan,
    compile: hasNewPlan ? (outcome.compile as CompileResponse) : state.compile,
    edits: nextEdits,
  };
  return { applied: true, state: nextState, historyNode, episode };
}

/**
 * One-click revert (item 18): restore the FULL snapshot, including
 * `semanticActions` (the split-brain fix), and mint a fresh `baseRevision`
 * rather than reusing the node's stored one -- a revert is itself a state
 * change and must look strictly newer than whatever produced the state being
 * reverted, exactly like any other local edit (integration spec §3).
 */
export function revertToHistoryNode(
  node: HistoryNode,
  currentBaseRevision: number,
): LiveState & { plan: AuthoredPlan; compile: CompileResponse } {
  return {
    baseRevision: currentBaseRevision + 1,
    semanticActions: node.semanticActions,
    plan: node.plan,
    compile: node.compile,
    edits: node.edits,
  };
}
