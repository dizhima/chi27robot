import { afterEach, describe, expect, it, vi } from "vitest";
import { runConversationTurn, type ResolveGate } from "./conversationRunner";
import { requestAuthor, applyRobotOverrides } from "../plan/authorPlan";
import { requestConflictResolution } from "../plan/resolveConflicts";
import type { ConversationMessage } from "./conversationTypes";
import type { AuthoredPlan } from "../plan/planTypes";

vi.mock("../plan/authorPlan", async () => {
  const actual = await vi.importActual<typeof import("../plan/authorPlan")>("../plan/authorPlan");
  return {
    ...actual,
    requestAuthor: vi.fn(),
    applyRobotOverrides: vi.fn((actions) => actions),
  };
});
vi.mock("../plan/resolveConflicts", () => ({
  requestConflictResolution: vi.fn(),
}));

const mockRequestAuthor = vi.mocked(requestAuthor);
const mockApplyRobotOverrides = vi.mocked(applyRobotOverrides);
const mockRequestConflictResolution = vi.mocked(requestConflictResolution);

afterEach(() => {
  vi.clearAllMocks();
});

const passingGate: ResolveGate = {
  status: "ready",
  dirty: false,
  inSync: true,
  delegableConflictCount: 2,
};

const plan: AuthoredPlan = { tasks: [] };

const baseArgs = {
  text: "hello",
  messages: [{ id: "u1", role: "user", content: "hello" }] as ConversationMessage[],
  semanticActions: [],
  draftPlan: null,
  completed: {},
  compileId: "compile-1",
};

describe("runConversationTurn", () => {
  it("routes intentHint resolve with a passing gate to requestConflictResolution and returns resolve_result", async () => {
    mockRequestConflictResolution.mockResolvedValue({
      plan,
      report: {
        rounds_used: 1,
        round_cap: 5,
        converged: true,
        applied: [],
        rejected: [],
        deferred: [],
        unresolved: [],
        escalated_to_human: [],
      },
      message: "resolved",
      compile: { schedule: [], warnings: [], conflicts: [], completed: {} },
    });

    const outcome = await runConversationTurn({
      ...baseArgs,
      intentHint: "resolve",
      gate: passingGate,
    });

    expect(mockRequestConflictResolution).toHaveBeenCalledWith({}, "compile-1", {
      signal: undefined,
    });
    expect(outcome).toEqual({
      kind: "resolve_result",
      plan,
      compile: { schedule: [], warnings: [], conflicts: [], completed: {} },
      message: "resolved",
    });
  });

  it.each([
    { ...passingGate, dirty: true },
    { ...passingGate, delegableConflictCount: 0 },
    { ...passingGate, inSync: false },
    { ...passingGate, status: "compiling" },
  ])("returns an answer and does not call the resolver when the gate fails (%o)", async (gate) => {
    const outcome = await runConversationTurn({
      ...baseArgs,
      intentHint: "resolve",
      gate,
    });

    expect(mockRequestConflictResolution).not.toHaveBeenCalled();
    expect(outcome.kind).toBe("answer");
  });

  it("routes the default (no hint) to requestAuthor and returns author_result", async () => {
    mockRequestAuthor.mockResolvedValue({
      actions: [],
      plan,
      message: "authored",
      reason: null,
    });

    const outcome = await runConversationTurn({
      ...baseArgs,
      intentHint: null,
      gate: passingGate,
    });

    expect(mockRequestAuthor).toHaveBeenCalledTimes(1);
    expect(mockRequestConflictResolution).not.toHaveBeenCalled();
    expect(outcome).toEqual({
      kind: "author_result",
      actions: [],
      plan,
      message: "authored",
      reason: null,
    });
  });

  it("excludes resolver- and explain-tagged messages from the array passed to requestAuthor", async () => {
    mockRequestAuthor.mockResolvedValue({ actions: [], plan: null, message: "ok", reason: null });

    const messages: ConversationMessage[] = [
      { id: "u1", role: "user", content: "move apple" },
      { id: "a1", role: "assistant", content: "resolved conflicts", source: "resolver" },
      { id: "a2", role: "assistant", content: "explanation", source: "explain" },
      { id: "a3", role: "assistant", content: "plan updated", source: "authoring" },
    ];

    await runConversationTurn({
      ...baseArgs,
      messages,
      intentHint: null,
      gate: passingGate,
    });

    const passedMessages = mockRequestAuthor.mock.calls[0][0];
    expect(passedMessages).toEqual([
      { role: "user", content: "move apple", source: undefined },
      { role: "assistant", content: "plan updated", source: "authoring" },
    ]);
  });

  it("applies robot overrides before calling requestAuthor", async () => {
    mockRequestAuthor.mockResolvedValue({ actions: [], plan: null, message: "ok", reason: null });
    const semanticActions = [{ id: "a1", robot: "robot0" as const, op: "move" as const }];

    await runConversationTurn({
      ...baseArgs,
      semanticActions,
      draftPlan: plan,
      intentHint: null,
      gate: passingGate,
    });

    expect(mockApplyRobotOverrides).toHaveBeenCalledWith(semanticActions, plan);
  });

  it("propagates a client rejection instead of swallowing it", async () => {
    mockRequestAuthor.mockRejectedValue(new Error("author down"));

    await expect(
      runConversationTurn({ ...baseArgs, intentHint: null, gate: passingGate }),
    ).rejects.toThrow("author down");
  });
});
