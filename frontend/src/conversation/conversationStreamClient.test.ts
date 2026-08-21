import { afterEach, describe, expect, it, vi } from "vitest";
import { streamConversationTurn, type StreamEvent } from "./conversationStreamClient";
import { runConversationTurn } from "./conversationRunner";
import { applyRobotOverrides, type AugmentedAction } from "../plan/authorPlan";
import { commitTurnResult, revertToHistoryNode, type LiveState } from "../plan/turnState";
import { EMPTY_VERSION_HISTORY, appendPlanVersion, snapshotForVersion } from "../plan/versionHistory";
import type { ConversationMessage } from "./conversationTypes";
import type { AuthoredPlan } from "../plan/planTypes";

vi.mock("./conversationRunner", async () => {
  const actual = await vi.importActual<typeof import("./conversationRunner")>("./conversationRunner");
  return {
    ...actual,
    runConversationTurn: vi.fn(),
  };
});
vi.mock("../plan/authorPlan", async () => {
  const actual = await vi.importActual<typeof import("../plan/authorPlan")>("../plan/authorPlan");
  return {
    ...actual,
    applyRobotOverrides: vi.fn((actions) => actions),
  };
});

const mockRunConversationTurn = vi.mocked(runConversationTurn);
const mockApplyRobotOverrides = vi.mocked(applyRobotOverrides);

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

const plan: AuthoredPlan = { tasks: [] };

const baseArgs = {
  text: "move apple",
  messages: [{ id: "u1", role: "user", content: "move apple" }] as ConversationMessage[],
  semanticActions: [],
  draftPlan: null,
  completed: {},
  compileId: "compile-1",
  gate: { status: "ready", dirty: false, inSync: true, delegableConflictCount: 0 },
  conflicts: [],
  warnings: [],
  lastResolverReport: null,
  turnId: "turn-1",
  baseRevision: 0,
};

/** Build a ReadableStream<Uint8Array> from raw text chunks (already possibly
 * splitting lines mid-JSON), simulating what `fetch`'s body reader yields. */
function streamFromChunks(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(encoder.encode(chunks[i]));
        i += 1;
      } else {
        controller.close();
      }
    },
  });
}

function mockFetchWithStream(body: ReadableStream<Uint8Array>, ok = true, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok,
    status,
    body,
  } as unknown as Response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const resultArtifact = {
  kind: "turn_result" as const,
  actions: [{ id: "a1", robot: "robot0", op: "move" }],
  plan,
  compile: { schedule: [], warnings: [], conflicts: [], completed: {} },
  base_revision: 0,
  stages: ["authoring", "compiling", "verified"],
};

function eventLines(): string[] {
  const events: StreamEvent[] = [
    { type: "message_started", turn_id: "turn-1", seq: 1 },
    { type: "intent_selected", turn_id: "turn-1", seq: 2, intent: "author", text: "Understanding your plan change." },
    { type: "progress", turn_id: "turn-1", seq: 3, stage: "augment", text: "Filling in shared-facility steps." },
    { type: "progress", turn_id: "turn-1", seq: 4, stage: "decomposition", text: "Plan drafted; compiling." },
    { type: "progress", turn_id: "turn-1", seq: 5, stage: "verified", text: "Verified." },
    { type: "result", turn_id: "turn-1", seq: 6, artifact: resultArtifact },
    { type: "message_completed", turn_id: "turn-1", seq: 7, text: "authored via stream" },
  ];
  return events.map((e) => JSON.stringify(e) + "\n");
}

describe("NDJSON line parsing", () => {
  it("parses every event exactly once, in order, even when chunks split a line mid-JSON", async () => {
    const lines = eventLines();
    const full = lines.join("");
    // Split the joined text at arbitrary byte offsets that land inside lines,
    // including mid-JSON, to simulate TCP chunking.
    const splitPoints = [10, 45, 90, full.length - 30];
    const chunks: string[] = [];
    let prev = 0;
    for (const p of splitPoints) {
      if (p > prev && p < full.length) {
        chunks.push(full.slice(prev, p));
        prev = p;
      }
    }
    chunks.push(full.slice(prev));

    mockFetchWithStream(streamFromChunks(chunks));
    const received: StreamEvent[] = [];

    const outcome = await streamConversationTurn(
      { ...baseArgs, intentHint: null },
      (e) => received.push(e),
    );

    expect(received.map((e) => e.seq)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(received.map((e) => e.type)).toEqual([
      "message_started",
      "intent_selected",
      "progress",
      "progress",
      "progress",
      "result",
      "message_completed",
    ]);
    expect(outcome).toEqual({
      kind: "turn_result",
      plan: resultArtifact.plan,
      compile: resultArtifact.compile,
      report: undefined,
      actions: resultArtifact.actions,
      baseRevision: resultArtifact.base_revision,
      message: "authored via stream",
    });
  });
});

describe("streamConversationTurn", () => {
  it("author prompt -> resolved committed plan with no intermediate user action (criterion 1): fires onEvent per event and resolves with the artifact as turn_result", async () => {
    mockFetchWithStream(streamFromChunks(eventLines()));
    const received: StreamEvent[] = [];

    const outcome = await streamConversationTurn(
      { ...baseArgs, intentHint: null },
      (e) => received.push(e),
    );

    expect(received).toHaveLength(7);
    expect(outcome).toEqual({
      kind: "turn_result",
      plan: resultArtifact.plan,
      compile: resultArtifact.compile,
      report: undefined,
      actions: resultArtifact.actions,
      baseRevision: resultArtifact.base_revision,
      message: "authored via stream",
    });
  });

  it("maps a semantic no-op without requiring a replacement plan or compile", async () => {
    const lines: StreamEvent[] = [
      { type: "message_started", turn_id: "turn-1", seq: 1 },
      {
        type: "result",
        turn_id: "turn-1",
        seq: 2,
        artifact: {
          kind: "no_change_result",
          base_revision: 4,
          author_message: "Both mugs are already scheduled for the sink.",
          authoring_summary: "No semantic actions changed.",
          reason: null,
        },
      },
      {
        type: "message_completed",
        turn_id: "turn-1",
        seq: 3,
        text: "Both mugs are already scheduled for the sink.",
      },
    ];
    mockFetchWithStream(
      streamFromChunks(lines.map((event) => `${JSON.stringify(event)}\n`)),
    );

    const outcome = await streamConversationTurn(
      { ...baseArgs, intentHint: null, baseRevision: 4 },
      () => {},
    );

    expect(outcome).toEqual({
      kind: "no_change_result",
      baseRevision: 4,
      authorMessage: "Both mugs are already scheduled for the sink.",
      authoringSummary: "No semantic actions changed.",
      reason: null,
      message: "Both mugs are already scheduled for the sink.",
    });
  });

  it("builds the unified request body: plan_state, messages filter, current_actions, and posts to /conversation/stream", async () => {
    const fetchMock = mockFetchWithStream(streamFromChunks(eventLines()));
    const messages: ConversationMessage[] = [
      { id: "u1", role: "user", content: "move apple" },
      { id: "a1", role: "assistant", content: "resolved conflicts", source: "resolver" },
      { id: "a2", role: "assistant", content: "explanation", source: "explain" },
      { id: "a3", role: "assistant", content: "plan updated", source: "authoring" },
      { id: "a4", role: "assistant", content: "full visible plan", excludeFromModel: true },
    ];
    const lastResolverReport = { converged: true };

    await streamConversationTurn(
      {
        ...baseArgs,
        intentHint: null,
        messages,
        gate: { status: "ready", dirty: false, inSync: true, delegableConflictCount: 3 },
        conflicts: [{ id: "c1" }],
        warnings: [{ id: "w1" }],
        lastResolverReport,
        baseRevision: 4,
      },
      () => {},
    );

    expect(mockApplyRobotOverrides).toHaveBeenCalledWith(baseArgs.semanticActions, baseArgs.draftPlan);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain("/conversation/stream");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({
      "Content-Type": "application/json",
      Accept: "application/x-ndjson",
    });

    const body = JSON.parse(init.body as string);
    expect(body.turn_id).toBe("turn-1");
    expect(body.intent_hint).toBeNull();
    // Exclude resolver/explain-tagged and display-only messages.
    expect(body.messages).toEqual([
      { role: "user", content: "move apple", source: undefined },
      { role: "assistant", content: "plan updated", source: "authoring" },
    ]);
    expect(body.current_actions).toEqual(baseArgs.semanticActions);
    expect(body.plan_state).toEqual({
      status: "ready",
      dirty: false,
      in_sync: true,
      delegable_conflict_count: 3,
      completed_plan: baseArgs.completed,
      compile_id: baseArgs.compileId,
      draft_plan: baseArgs.draftPlan,
      conflicts: [{ id: "c1" }],
      warnings: [{ id: "w1" }],
      last_resolver_report: lastResolverReport,
    });
    // item 14: base_revision travels with every turn, and `protected`/`edits`
    // are always sent (empty by default when the caller supplies neither).
    expect(body.base_revision).toBe(4);
    expect(body.protected).toEqual({});
    expect(body.edits).toEqual([]);
    expect(body.plan_refs).toEqual([]);
  });

  it("makes v2 baseline authoring stateless while preserving scene grounding", async () => {
    const fetchMock = mockFetchWithStream(streamFromChunks(eventLines()));
    const oldPlan: AuthoredPlan = {
      tasks: [{ task: "old-task", robot: "robot0", steps: [] }],
    };
    const oldActions: AugmentedAction[] = [
      { id: "old-action", robot: "robot0", op: "move" },
    ];
    const messages: ConversationMessage[] = [
      { id: "u0", role: "user", content: "old instruction" },
      { id: "a0", role: "assistant", content: "old plan result", source: "authoring" },
      { id: "u1", role: "user", content: "complete new task specification" },
    ];

    await streamConversationTurn(
      {
        ...baseArgs,
        text: "complete new task specification",
        intentHint: null,
        statelessAuthoring: true,
        messages,
        semanticActions: oldActions,
        draftPlan: oldPlan,
        previousPlan: oldPlan,
        completed: { robot0: oldPlan.tasks[0].steps },
        compileId: "old-compile",
        conflicts: [{ id: "old-conflict" }],
        warnings: [{ id: "old-warning" }],
        lastResolverReport: { converged: true },
        planRefs: [{
          kind: "plan_task",
          id: "old-ref",
          actionId: "old-action",
          label: "old task",
          robot: "robot0",
          op: "move",
        }],
        sceneRefs: [{ kind: "facility", id: "scene-object", name: "fridge" }],
        edits: [{
          op: "move_task",
          target: { actionId: "old-task" },
          robot: "robot1",
          afterActionId: null,
        }],
        protected: {
          allocations: [{ group: "old-task", robot: "robot0" }],
          orderings: [],
          destinations: [],
          waypoints: [],
        },
      },
      () => {},
    );

    expect(mockApplyRobotOverrides).not.toHaveBeenCalled();
    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body.messages).toEqual([
      { role: "user", content: "complete new task specification" },
    ]);
    expect(body.current_actions).toEqual([]);
    expect(body.previous_plan).toBeNull();
    expect(body.plan_refs).toEqual([]);
    expect(body.edits).toEqual([]);
    expect(body.protected).toEqual({});
    expect(body.plan_state).toEqual({
      status: "idle",
      dirty: false,
      in_sync: false,
      delegable_conflict_count: 0,
      completed_plan: {},
      compile_id: null,
      draft_plan: null,
      conflicts: [],
      warnings: [],
      last_resolver_report: null,
    });
    expect(body.scene_refs).toHaveLength(1);
  });

  it("serializes semantic plan refs separately from scene_refs", async () => {
    const fetchMock = mockFetchWithStream(streamFromChunks(eventLines()));
    await streamConversationTurn(
      {
        ...baseArgs,
        intentHint: null,
        sceneRefs: [],
        planRefs: [{
          kind: "plan_task",
          id: "local-token",
          actionId: "move_milk_fridge",
          label: "milk → fridge",
          robot: "robot1",
          op: "move",
          object: "milk",
          dest: "fridge",
        }],
        planRefHandles: new Map([["local-token", "t1"]]),
      },
      () => {},
    );
    const [, init] = fetchMock.mock.calls[0];
    const body = JSON.parse(init.body as string);
    expect(body.scene_refs).toEqual([]);
    expect(body.plan_refs).toEqual([
      { kind: "plan_task", handle: "t1", action_id: "move_milk_fridge" },
    ]);
  });

  it("sends the caller's edits and protected set (S3 item 16)", async () => {
    const fetchMock = mockFetchWithStream(streamFromChunks(eventLines()));
    const edits = [
      { op: "set_task_robot" as const, target: { actionId: "t0" }, robot: "robot1" as const },
    ];
    const protectedSet = { allocations: [{ group: "t0", robot: "robot1" as const }], orderings: [], destinations: [], waypoints: [] };

    await streamConversationTurn(
      { ...baseArgs, intentHint: "edit" as const, edits, protected: protectedSet },
      () => {},
    );

    const [, init] = fetchMock.mock.calls[0];
    const body = JSON.parse(init.body as string);
    expect(body.intent_hint).toBe("edit");
    expect(body.edits).toEqual(edits);
    expect(body.protected).toEqual(protectedSet);
  });

  it("surfaces dropped_edits from the artifact as droppedEdits on the outcome", async () => {
    const artifactWithDrops = {
      ...resultArtifact,
      dropped_edits: [{ op: "set_step_at", target: { stepId: "ghost:s0" } }],
    };
    const lines: StreamEvent[] = [
      { type: "message_started", turn_id: "turn-1", seq: 1 },
      { type: "result", turn_id: "turn-1", seq: 2, artifact: artifactWithDrops },
      { type: "message_completed", turn_id: "turn-1", seq: 3, text: "done" },
    ];
    mockFetchWithStream(streamFromChunks(lines.map((e) => JSON.stringify(e) + "\n")));

    const outcome = await streamConversationTurn({ ...baseArgs, intentHint: null }, () => {});
    expect(outcome.kind).toBe("turn_result");
    if (outcome.kind !== "turn_result") throw new Error("unreachable");
    expect(outcome.droppedEdits).toEqual(artifactWithDrops.dropped_edits);
  });

  it("rejects with the error event's text and never resolves an outcome", async () => {
    const lines = [
      JSON.stringify({ type: "message_started", turn_id: "turn-1", seq: 1 }) + "\n",
      JSON.stringify({ type: "error", turn_id: "turn-1", seq: 2, text: "authoring failed: boom" }) + "\n",
    ];
    mockFetchWithStream(streamFromChunks(lines));
    const received: StreamEvent[] = [];

    await expect(
      streamConversationTurn({ ...baseArgs, intentHint: null }, (e) => received.push(e)),
    ).rejects.toThrow("authoring failed: boom");
  });

  it("throws when the stream ends without a result event", async () => {
    const lines = [
      JSON.stringify({ type: "message_started", turn_id: "turn-1", seq: 1 }) + "\n",
      JSON.stringify({ type: "intent_selected", turn_id: "turn-1", seq: 2, text: "Understanding your plan change." }) + "\n",
    ];
    mockFetchWithStream(streamFromChunks(lines));

    await expect(streamConversationTurn({ ...baseArgs, intentHint: null }, () => {})).rejects.toThrow(
      /result/,
    );
  });

  // Phase 3 had a "resolve gate-fail returns an answer outcome and does NOT
  // open the stream" test here, asserting the CLIENT gated resolve before
  // ever calling fetch. Phase 4 moves that gate to the backend (see
  // docs/phase4_spec_classifier_explain_activities.md section 6.1/6.2): the
  // client now ALWAYS opens the stream for every intent, including a
  // gate-failing resolve, and the backend responds with an `answer` artifact
  // (the precondition message) instead of a `resolve_result`. This test
  // replaces the old one: it asserts the stream IS opened even though the
  // gate fails, and that the returned `answer` artifact is surfaced as-is.
  it("resolve with a failing gate still opens the stream now (backend gates); an answer artifact surfaces as {kind:'answer'}", async () => {
    const lines: StreamEvent[] = [
      { type: "message_started", turn_id: "turn-1", seq: 1 },
      { type: "intent_selected", turn_id: "turn-1", seq: 2, intent: "resolve", text: "Checking eligibility." },
      {
        type: "result",
        turn_id: "turn-1",
        seq: 3,
        artifact: {
          kind: "answer",
          turn_id: "turn-1",
          content:
            "Can't resolve yet: need a compiled, in-sync plan with at least one delegable conflict. Compile the current draft first.",
        },
      },
      { type: "message_completed", turn_id: "turn-1", seq: 4, text: "precondition" },
    ];
    const fetchMock = mockFetchWithStream(streamFromChunks(lines.map((e) => JSON.stringify(e) + "\n")));

    const args = {
      ...baseArgs,
      intentHint: "resolve" as const,
      gate: { status: "ready", dirty: false, inSync: true, delegableConflictCount: 0 },
    };
    const outcome = await streamConversationTurn(args, () => {});

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(mockRunConversationTurn).not.toHaveBeenCalled();
    expect(outcome).toEqual({
      kind: "answer",
      intent: "explain",
      content:
        "Can't resolve yet: need a compiled, in-sync plan with at least one delegable conflict. Compile the current draft first.",
    });
  });

  it("sync (Resolve button) happy path under COMPOUND_TURN=1: streams progress and resolves turn_result with the artifact's plan/compile/report", async () => {
    // D3 (compound_turn_integration_spec.md §2): under the compound flag the
    // backend rewrites a button "resolve" into a sync-hint tail run, so it
    // now terminates in a turn_result, not the legacy resolve_result.
    const turnResultArtifact = {
      kind: "turn_result" as const,
      turn_id: "turn-1",
      plan,
      compile: { schedule: [], warnings: [], conflicts: [], completed: {} },
      report: { converged: true },
      authoring_summary: "Authoring operations:\n1. augment returned: t0:close (close fridge) -> robot0.",
      edit_message: "Applied your edit:\n1. Reordered orange_1 → fridge after apple_2 → fridge on robot0.",
      resolve_summary: "Resolved all conflicts with 1 adjustment.",
      base_revision: 7,
    };
    const lines: StreamEvent[] = [
      { type: "message_started", turn_id: "turn-1", seq: 1 },
      { type: "intent_selected", turn_id: "turn-1", seq: 2, intent: "resolve", text: "Checking the compiled plan for conflicts." },
      { type: "progress", turn_id: "turn-1", seq: 3, stage: "run_started", text: "Checking conflicts in the compiled plan." },
      { type: "progress", turn_id: "turn-1", seq: 4, stage: "run_completed", text: "Final verification done; producing the summary." },
      { type: "result", turn_id: "turn-1", seq: 5, artifact: turnResultArtifact },
      { type: "message_completed", turn_id: "turn-1", seq: 6, text: "Resolved conflicts." },
    ];
    const fetchMock = mockFetchWithStream(
      streamFromChunks(lines.map((e) => JSON.stringify(e) + "\n")),
    );
    const received: StreamEvent[] = [];

    const args = {
      ...baseArgs,
      intentHint: "resolve" as const,
      gate: { status: "ready", dirty: false, inSync: true, delegableConflictCount: 2 },
      baseRevision: 7,
    };
    const outcome = await streamConversationTurn(args, (e) => received.push(e));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain("/conversation/stream");
    const body = JSON.parse(init.body as string);
    expect(body.intent_hint).toBe("resolve");
    expect(body.plan_state.completed_plan).toEqual(baseArgs.completed);
    expect(body.plan_state.compile_id).toBe(baseArgs.compileId);
    expect(body.base_revision).toBe(7);

    expect(received).toHaveLength(6);
    expect(outcome).toEqual({
      kind: "turn_result",
      plan: turnResultArtifact.plan,
      compile: turnResultArtifact.compile,
      report: turnResultArtifact.report,
      actions: undefined,
      baseRevision: 7,
      editMessage: turnResultArtifact.edit_message,
      authoringSummary: turnResultArtifact.authoring_summary,
      resolveSummary: turnResultArtifact.resolve_summary,
      message: "Resolved conflicts.",
    });
  });

  it("a non-converged turn_result still commits (surfaces as a normal outcome, not an error)", async () => {
    const turnResultArtifact = {
      kind: "turn_result" as const,
      turn_id: "turn-1",
      plan,
      compile: { schedule: [], warnings: ["2 conflicts remain"], conflicts: [], completed: {} },
      report: { converged: false, unresolved: [{ reason: "no_remaining_legal_strategy" }] },
      base_revision: 0,
    };
    const lines: StreamEvent[] = [
      { type: "message_started", turn_id: "turn-1", seq: 1 },
      { type: "result", turn_id: "turn-1", seq: 2, artifact: turnResultArtifact },
      { type: "message_completed", turn_id: "turn-1", seq: 3, text: "Fixed some conflicts; some remain." },
    ];
    mockFetchWithStream(streamFromChunks(lines.map((e) => JSON.stringify(e) + "\n")));

    const outcome = await streamConversationTurn({ ...baseArgs, intentHint: null }, () => {});

    expect(outcome).toEqual({
      kind: "turn_result",
      plan: turnResultArtifact.plan,
      compile: turnResultArtifact.compile,
      report: turnResultArtifact.report,
      actions: undefined,
      baseRevision: 0,
      message: "Fixed some conflicts; some remain.",
    });
  });

  it("throws an actionable COMPOUND_TURN error for a legacy author_result artifact", async () => {
    const lines: StreamEvent[] = [
      { type: "message_started", turn_id: "turn-1", seq: 1 },
      {
        type: "result",
        turn_id: "turn-1",
        seq: 2,
        artifact: { kind: "author_result", actions: [], plan, message: "authored", reason: null },
      },
      { type: "message_completed", turn_id: "turn-1", seq: 3, text: "authored" },
    ];
    mockFetchWithStream(streamFromChunks(lines.map((e) => JSON.stringify(e) + "\n")));

    await expect(
      streamConversationTurn({ ...baseArgs, intentHint: null }, () => {}),
    ).rejects.toThrow(/COMPOUND_TURN=1/);
  });

  it("throws an actionable COMPOUND_TURN error for a legacy resolve_result artifact", async () => {
    const lines: StreamEvent[] = [
      { type: "message_started", turn_id: "turn-1", seq: 1 },
      {
        type: "result",
        turn_id: "turn-1",
        seq: 2,
        artifact: { kind: "resolve_result", plan, report: {} },
      },
      { type: "message_completed", turn_id: "turn-1", seq: 3, text: "resolved" },
    ];
    mockFetchWithStream(streamFromChunks(lines.map((e) => JSON.stringify(e) + "\n")));

    await expect(
      streamConversationTurn({ ...baseArgs, intentHint: "resolve" as const }, () => {}),
    ).rejects.toThrow(/COMPOUND_TURN=1/);
  });

  it("a stale base_revision artifact is discarded, not applied (via commitTurnResult)", async () => {
    const staleArtifact = {
      kind: "turn_result" as const,
      plan,
      compile: { schedule: [], warnings: [], conflicts: [], completed: {} },
      base_revision: 2, // this turn was sent against revision 2...
    };
    const lines: StreamEvent[] = [
      { type: "message_started", turn_id: "turn-1", seq: 1 },
      { type: "result", turn_id: "turn-1", seq: 2, artifact: staleArtifact },
      { type: "message_completed", turn_id: "turn-1", seq: 3, text: "done" },
    ];
    mockFetchWithStream(streamFromChunks(lines.map((e) => JSON.stringify(e) + "\n")));

    const outcome = await streamConversationTurn({ ...baseArgs, intentHint: null, baseRevision: 2 }, () => {});
    expect(outcome.kind).toBe("turn_result");
    if (outcome.kind !== "turn_result") throw new Error("unreachable");

    // ...but by the time it lands, a local edit/commit already moved the
    // live state to revision 5. The caller (ScenePage, via commitTurnResult)
    // must discard this reply rather than apply it.
    const currentState: LiveState = { baseRevision: 5, semanticActions: [], plan: null, compile: null, edits: [] };
    const result = commitTurnResult(currentState, {
      plan: outcome.plan,
      compile: outcome.compile,
      actions: outcome.actions,
      baseRevision: outcome.baseRevision,
    });
    expect(result.applied).toBe(false);
  });

  it("revert restores plan AND semanticActions, and the next turn's request body carries the restored actions", async () => {
    const oldActions = [{ id: "a1", robot: "robot0" as const, op: "move" as const }];
    const newActions = [
      { id: "a1", robot: "robot0" as const, op: "move" as const },
      { id: "a2", robot: "robot1" as const, op: "move" as const },
    ];
    const oldPlan: AuthoredPlan = { tasks: [{ robot: "robot0", steps: [{ id: "old:s0", op: "wait" }] }] };
    const oldCompile = { schedule: [], warnings: [], conflicts: [], completed: {} };
    const newPlan: AuthoredPlan = { tasks: [{ robot: "robot1", steps: [{ id: "new:s0", op: "wait" }] }] };
    const newCompile = { schedule: [], warnings: [], conflicts: [], completed: {} };

    const stateBeforeTurn: LiveState = {
      baseRevision: 1,
      semanticActions: oldActions,
      plan: oldPlan,
      compile: oldCompile,
      edits: [],
    };
    const committed = commitTurnResult(stateBeforeTurn, {
      plan: newPlan,
      compile: newCompile,
      actions: newActions,
      baseRevision: 1,
    });
    if (!committed.applied || !committed.historyNode) throw new Error("expected a committed history node");
    const reverted = revertToHistoryNode(committed.historyNode, committed.state.baseRevision);

    expect(reverted.plan).toEqual(oldPlan);
    expect(reverted.semanticActions).toEqual(oldActions);
    expect(reverted.semanticActions).not.toEqual(newActions);

    // The split-brain check: the NEXT turn must carry the restored actions.
    const fetchMock = mockFetchWithStream(streamFromChunks(eventLines()));
    await streamConversationTurn(
      { ...baseArgs, intentHint: null, semanticActions: reverted.semanticActions, baseRevision: reverted.baseRevision },
      () => {},
    );
    const [, init] = fetchMock.mock.calls[0];
    const body = JSON.parse(init.body as string);
    expect(body.current_actions).toEqual(oldActions);
    expect(body.current_actions).not.toEqual(newActions);
    expect(body.base_revision).toBe(reverted.baseRevision);
  });

  it("chat and sync requests use only the checked-out version's plan and conversation", async () => {
    const oldActions = [{ id: "old-action", robot: "robot0" as const, op: "move" as const }];
    const oldPlan: AuthoredPlan = {
      tasks: [{ task: "old-action", robot: "robot0", steps: [{ id: "old:s0", op: "wait" }] }],
    };
    const oldCompile = { schedule: [], warnings: [], conflicts: [], completed: {} };
    const oldMessages: ConversationMessage[] = [
      { id: "old-user", role: "user", content: "build the old plan" },
      { id: "old-assistant", role: "assistant", content: "old plan ready", source: "authoring" },
    ];
    let history = appendPlanVersion(EMPTY_VERSION_HISTORY, {
      id: "v1",
      createdAt: 1,
      source: "chat",
      title: "old",
      snapshot: {
        plan: oldPlan,
        compile: oldCompile,
        semanticActions: oldActions,
        edits: [],
        messages: oldMessages,
        lastResolverReport: null,
      },
    });
    history = appendPlanVersion(history, {
      id: "v2",
      createdAt: 2,
      source: "chat",
      title: "future",
      snapshot: {
        plan: { tasks: [{ task: "future", robot: "robot1", steps: [] }] },
        compile: oldCompile,
        semanticActions: [{ id: "future", robot: "robot1", op: "move" }],
        edits: [],
        messages: [...oldMessages, { id: "future-user", role: "user", content: "future branch instruction" }],
        lastResolverReport: null,
      },
    });
    const restored = snapshotForVersion(history, "v1")!;

    for (const intentHint of [null, "edit"] as const) {
      const fetchMock = mockFetchWithStream(streamFromChunks(eventLines()));
      await streamConversationTurn(
        {
          ...baseArgs,
          text: intentHint === null ? "continue here" : "",
          intentHint,
          messages: restored.messages,
          semanticActions: restored.semanticActions,
          draftPlan: restored.plan,
          previousPlan: restored.plan,
          completed: restored.compile.completed,
          baseRevision: 9,
        },
        () => {},
      );
      const [, init] = fetchMock.mock.calls[0];
      const body = JSON.parse(init.body as string);
      expect(body.previous_plan).toEqual(oldPlan);
      expect(body.plan_state.draft_plan).toEqual(oldPlan);
      expect(body.current_actions).toEqual(oldActions);
      expect(body.messages.map((message: ConversationMessage) => message.content)).toEqual([
        "build the old plan",
        "old plan ready",
      ]);
      expect(JSON.stringify(body)).not.toContain("future branch instruction");
    }
  });

  it("an explain answer terminal resolves {kind:'answer', intent:'explain', content}", async () => {
    const answerArtifact = {
      kind: "answer" as const,
      turn_id: "turn-1",
      content: "There are 2 conflicts left on robot1's track.",
      intent: "explain",
      follow_up: false,
    };
    const lines: StreamEvent[] = [
      { type: "message_started", turn_id: "turn-1", seq: 1 },
      { type: "intent_selected", turn_id: "turn-1", seq: 2, intent: "explain", text: "Looking into your question." },
      { type: "progress", turn_id: "turn-1", seq: 3, stage: "read_conflicts", text: "Checking the current conflicts." },
      { type: "result", turn_id: "turn-1", seq: 4, artifact: answerArtifact },
      { type: "message_completed", turn_id: "turn-1", seq: 5, text: answerArtifact.content },
    ];
    mockFetchWithStream(streamFromChunks(lines.map((e) => JSON.stringify(e) + "\n")));

    const outcome = await streamConversationTurn({ ...baseArgs, intentHint: null }, () => {});

    expect(outcome).toEqual({
      kind: "answer",
      intent: "explain",
      content: answerArtifact.content,
    });
  });
});
