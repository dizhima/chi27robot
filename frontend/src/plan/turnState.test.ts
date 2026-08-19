import { describe, expect, it } from "vitest";
import { commitTurnResult, revertToHistoryNode, type HistoryNode, type LiveState } from "./turnState";
import type { AuthoredPlan, CompileResponse } from "./planTypes";
import type { AugmentedAction } from "./authorPlan";
import type { PlanEditDelta } from "./planEdits";

const plan = (label: string): AuthoredPlan => ({
  tasks: [{ robot: "robot0", steps: [{ id: `${label}:s0`, op: "wait", duration: 1 }] }],
});
const compile = (label: string): CompileResponse => ({
  schedule: [],
  warnings: [],
  conflicts: [],
  completed: { robot0: [{ id: `${label}:s0`, op: "wait", duration: 1 }] },
});
const action = (id: string): AugmentedAction => ({ id, robot: "robot0", op: "move" });

const emptyState: LiveState = {
  baseRevision: 0,
  semanticActions: [],
  plan: null,
  compile: null,
  edits: [],
};

describe("commitTurnResult", () => {
  it("commits a resolved plan with no intermediate user action (criterion 1)", () => {
    const outcome = {
      plan: plan("p1"),
      compile: compile("p1"),
      actions: [action("a1")],
      baseRevision: 0,
    };
    const result = commitTurnResult(emptyState, outcome);
    expect(result.applied).toBe(true);
    if (!result.applied) throw new Error("unreachable");
    expect(result.state.plan).toBe(outcome.plan);
    expect(result.state.compile).toBe(outcome.compile);
    expect(result.state.semanticActions).toEqual([action("a1")]);
    expect(result.state.baseRevision).toBe(1);
    // First-ever commit: nothing existed before it, so there is nothing to
    // revert to.
    expect(result.historyNode).toBeNull();
  });

  it("discards a stale artifact instead of applying it", () => {
    const state: LiveState = { ...emptyState, baseRevision: 3, plan: plan("cur"), compile: compile("cur") };
    const outcome = { plan: plan("stale"), compile: compile("stale"), baseRevision: 1 };
    const result = commitTurnResult(state, outcome);
    expect(result.applied).toBe(false);
  });

  it("commits and creates a history node even when the resolver did not converge", () => {
    const state: LiveState = {
      baseRevision: 1,
      semanticActions: [action("a1")],
      plan: plan("p1"),
      compile: compile("p1"),
      edits: [],
    };
    const outcome = {
      plan: plan("p2"),
      compile: compile("p2"),
      actions: [action("a1"), action("a2")],
      baseRevision: 1,
      // report.converged === false lives on the caller's outcome, not on
      // this narrowed TurnResultOutcome — commitTurnResult never inspects
      // convergence at all, which is the point: it commits regardless.
    };
    const result = commitTurnResult(state, outcome);
    expect(result.applied).toBe(true);
    if (!result.applied) throw new Error("unreachable");
    expect(result.state.plan).toBe(outcome.plan);
    expect(result.historyNode).toEqual({
      revision: 1,
      plan: state.plan,
      compile: state.compile,
      semanticActions: state.semanticActions,
      edits: [],
    });
  });

  it("a no-actions/no-plan turn still bumps the revision and keeps the previous plan", () => {
    const state: LiveState = {
      baseRevision: 2,
      semanticActions: [action("a1")],
      plan: plan("p1"),
      compile: compile("p1"),
      edits: [],
    };
    const result = commitTurnResult(state, { plan: null, actions: [], baseRevision: 2 });
    expect(result.applied).toBe(true);
    if (!result.applied) throw new Error("unreachable");
    expect(result.state.plan).toBe(state.plan);
    expect(result.state.compile).toBe(state.compile);
    expect(result.state.baseRevision).toBe(3);
  });

  it("commits an explicit empty plan replacement after the last task is removed", () => {
    const state: LiveState = {
      baseRevision: 2,
      semanticActions: [action("a1")],
      plan: plan("p1"),
      compile: compile("p1"),
      edits: [],
    };
    const emptyPlan = { tasks: [] };
    const emptyCompile = { schedule: [], warnings: [], conflicts: [], completed: {} };

    const result = commitTurnResult(state, {
      plan: emptyPlan,
      compile: emptyCompile,
      actions: [],
      baseRevision: 2,
    });

    expect(result.applied).toBe(true);
    if (!result.applied) throw new Error("unreachable");
    expect(result.state.semanticActions).toEqual([]);
    expect(result.state.plan).toBe(emptyPlan);
    expect(result.state.compile).toBe(emptyCompile);
    expect(result.historyNode?.plan).toBe(state.plan);
    expect(result.historyNode?.semanticActions).toBe(state.semanticActions);
  });
});

describe("revertToHistoryNode", () => {
  it("restores plan AND semanticActions (split-brain fix) with a fresh baseRevision", () => {
    const original: LiveState = {
      baseRevision: 1,
      semanticActions: [action("a1")],
      plan: plan("p1"),
      compile: compile("p1"),
      edits: [],
    };
    const afterTurn = commitTurnResult(original, {
      plan: plan("p2"),
      compile: compile("p2"),
      actions: [action("a1"), action("a2")],
      baseRevision: 1,
    });
    expect(afterTurn.applied).toBe(true);
    if (!afterTurn.applied) throw new Error("unreachable");
    expect(afterTurn.historyNode).not.toBeNull();

    const reverted = revertToHistoryNode(afterTurn.historyNode!, afterTurn.state.baseRevision);
    // The plan goes back...
    expect(reverted.plan).toBe(original.plan);
    // ...and, critically, so do the semantic actions the next turn would
    // otherwise silently regenerate the newer plan from.
    expect(reverted.semanticActions).toEqual(original.semanticActions);
    expect(reverted.semanticActions).not.toEqual(afterTurn.state.semanticActions);
    // Monotone: strictly newer than the state being reverted, not a reuse of
    // the node's own older revision number.
    expect(reverted.baseRevision).toBeGreaterThan(afterTurn.state.baseRevision);
  });
});

describe("commitTurnResult episode coalescing (item 18)", () => {
  it("three consecutive edit-triggered commits produce exactly ONE history node", () => {
    let state: LiveState = { ...emptyState, plan: plan("p0"), compile: compile("p0") };
    let openEpisode: HistoryNode | null = null;

    const first = commitTurnResult(
      state,
      { plan: plan("p1"), compile: compile("p1"), baseRevision: state.baseRevision },
      { trigger: "edit", sentEdits: [], openEpisode },
    );
    if (!first.applied) throw new Error("unreachable");
    state = first.state;
    openEpisode = first.episode;
    expect(first.historyNode).toEqual({
      revision: 0,
      plan: plan("p0"),
      compile: compile("p0"),
      semanticActions: [],
      edits: [],
    });

    const second = commitTurnResult(
      state,
      { plan: plan("p2"), compile: compile("p2"), baseRevision: state.baseRevision },
      { trigger: "edit", sentEdits: [], openEpisode },
    );
    if (!second.applied) throw new Error("unreachable");
    state = second.state;
    openEpisode = second.episode;
    // Reused verbatim -- NOT re-snapshotted from `state` (which is now p1),
    // or the user's revert would only reach back to p1, one edit short of
    // what "before I started this episode" means.
    expect(second.historyNode).toBe(first.historyNode);

    const third = commitTurnResult(
      state,
      { plan: plan("p3"), compile: compile("p3"), baseRevision: state.baseRevision },
      { trigger: "edit", sentEdits: [], openEpisode },
    );
    if (!third.applied) throw new Error("unreachable");
    expect(third.historyNode).toBe(first.historyNode);
    expect(third.historyNode?.plan).toEqual(plan("p0"));
  });

  it("a chat turn after an open edit episode starts (and the episode after it starts) its own new node", () => {
    let state: LiveState = { ...emptyState, plan: plan("p0"), compile: compile("p0") };

    const editCommit = commitTurnResult(
      state,
      { plan: plan("p1"), compile: compile("p1"), baseRevision: state.baseRevision },
      { trigger: "edit", sentEdits: [], openEpisode: null },
    );
    if (!editCommit.applied) throw new Error("unreachable");
    state = editCommit.state;

    // A chat turn closes the edit episode: it snapshots the CURRENT state
    // (p1, what the edit episode left behind) rather than reusing the edit
    // episode's node (p0), and its returned `episode` is null.
    const chatCommit = commitTurnResult(
      state,
      { plan: plan("p2"), compile: compile("p2"), actions: [action("a1")], baseRevision: state.baseRevision },
      { trigger: "chat", openEpisode: editCommit.episode },
    );
    if (!chatCommit.applied) throw new Error("unreachable");
    expect(chatCommit.historyNode).not.toBe(editCommit.historyNode);
    expect(chatCommit.historyNode?.plan).toEqual(plan("p1"));
    expect(chatCommit.episode).toBeNull();

    // A NEW edit episode after the chat turn opens its own fresh node (p2),
    // not the stale one the chat turn just closed.
    const nextEditCommit = commitTurnResult(
      chatCommit.state,
      { plan: plan("p3"), compile: compile("p3"), baseRevision: chatCommit.state.baseRevision },
      { trigger: "edit", sentEdits: [], openEpisode: chatCommit.episode },
    );
    if (!nextEditCommit.applied) throw new Error("unreachable");
    expect(nextEditCommit.historyNode?.plan).toEqual(plan("p2"));
  });

  it("a sync press (no pending edits) closes an open edit episode exactly like a chat turn", () => {
    // The merged sync button's "no pending edits" branch (item 19) commits
    // with trigger "sync", not "chat" -- but it must close an open edit
    // episode the same way a chat turn does: there is no idle timer, only
    // these two visible triggers.
    let state: LiveState = { ...emptyState, plan: plan("p0"), compile: compile("p0") };

    const editCommit = commitTurnResult(
      state,
      { plan: plan("p1"), compile: compile("p1"), baseRevision: state.baseRevision },
      { trigger: "edit", sentEdits: [], openEpisode: null },
    );
    if (!editCommit.applied) throw new Error("unreachable");
    state = editCommit.state;

    const syncCommit = commitTurnResult(
      state,
      { plan: plan("p2"), compile: compile("p2"), baseRevision: state.baseRevision },
      { trigger: "sync", openEpisode: editCommit.episode },
    );
    if (!syncCommit.applied) throw new Error("unreachable");
    // Snapshots the CURRENT state (p1, what the edit episode left behind),
    // not the edit episode's own node (p0) -- same rule a chat turn follows.
    expect(syncCommit.historyNode).not.toBe(editCommit.historyNode);
    expect(syncCommit.historyNode?.plan).toEqual(plan("p1"));
    expect(syncCommit.episode).toBeNull();

    // A sync press afterwards (with a fresh edit episode) starts a new node,
    // not the one the prior sync press just closed.
    const nextEditCommit = commitTurnResult(
      syncCommit.state,
      { plan: plan("p3"), compile: compile("p3"), baseRevision: syncCommit.state.baseRevision },
      { trigger: "edit", sentEdits: [], openEpisode: syncCommit.episode },
    );
    if (!nextEditCommit.applied) throw new Error("unreachable");
    expect(nextEditCommit.historyNode?.plan).toEqual(plan("p2"));
  });
});

describe("commitTurnResult pending-edit clearing (D2b)", () => {
  const delta = (target: string): PlanEditDelta => ({
    op: "set_step_at",
    target: { stepId: target },
    at: [0, 0],
  });

  it("clears the sent batch on a successful commit -- it is not re-sent next turn", () => {
    const sent = [delta("s0")];
    const state: LiveState = { ...emptyState, plan: plan("p0"), compile: compile("p0"), edits: sent };
    const result = commitTurnResult(
      state,
      { plan: plan("p1"), compile: compile("p1"), baseRevision: state.baseRevision },
      { trigger: "edit", sentEdits: sent, openEpisode: null },
    );
    if (!result.applied) throw new Error("unreachable");
    // D2b's whole point: once baked into p1 (or dropped-with-warning), the
    // delta is gone from the pending list -- under the old "persistent
    // replay ledger" model this stayed non-empty forever and got replayed
    // (double-applied) onto every later turn's rebuilt plan.
    expect(result.state.edits).toEqual([]);
  });

  it("keeps a delta added WHILE the request was in flight -- it was never sent, so it isn't cleared", () => {
    const sentBeforeAwait = [delta("s0")];
    const addedDuringFlight = delta("s1");
    // Mirrors ScenePage: `state.edits` is read fresh (after the await), so it
    // already contains anything pushed to the pending list while the request
    // that sent `sentBeforeAwait` was in flight.
    const state: LiveState = {
      ...emptyState,
      plan: plan("p0"),
      compile: compile("p0"),
      edits: [...sentBeforeAwait, addedDuringFlight],
    };
    const result = commitTurnResult(
      state,
      { plan: plan("p1"), compile: compile("p1"), baseRevision: state.baseRevision },
      { trigger: "edit", sentEdits: sentBeforeAwait, openEpisode: null },
    );
    if (!result.applied) throw new Error("unreachable");
    expect(result.state.edits).toEqual([addedDuringFlight]);
  });

  it("a sync commit (nothing pending, sentEdits omitted) leaves the pending list untouched", () => {
    const state: LiveState = { ...emptyState, plan: plan("p0"), compile: compile("p0"), edits: [] };
    const result = commitTurnResult(
      state,
      { plan: plan("p1"), compile: compile("p1"), baseRevision: state.baseRevision },
      { trigger: "sync" },
    );
    if (!result.applied) throw new Error("unreachable");
    expect(result.state.edits).toEqual([]);
  });
});
