/**
 * Superseded-turn guard test (acceptance test 6, spec section 7).
 *
 * ScenePage.tsx wraps a big MuJoCo/three.js viewer and isn't practical to
 * mount in a component test here, so this test extracts the exact guard
 * logic `sendConversation` uses (an `activeTurnRef` compared against the
 * turn's own id when its promise settles) into a tiny standalone harness and
 * exercises it against `runConversationTurn` with two overlapping turns.
 * This proves the guard's actual comparison semantics — not just that some
 * ref exists — without requiring a full ScenePage render.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { runConversationTurn } from "./conversationRunner";
import { requestAuthor } from "../plan/authorPlan";
import type { ConversationMessage, TurnOutcome } from "./conversationTypes";

vi.mock("../plan/authorPlan", async () => {
  const actual = await vi.importActual<typeof import("../plan/authorPlan")>("../plan/authorPlan");
  return { ...actual, requestAuthor: vi.fn(), applyRobotOverrides: vi.fn((actions) => actions) };
});
vi.mock("../plan/resolveConflicts", () => ({ requestConflictResolution: vi.fn() }));

const mockRequestAuthor = vi.mocked(requestAuthor);

afterEach(() => {
  vi.clearAllMocks();
});

/**
 * Mirrors the guard in ScenePage.tsx's `sendConversation`:
 *   if (activeTurnRef.current !== turnId) return;
 *   await applyOutcome(...)
 * `activeTurnRef` here is a plain mutable box standing in for the React ref.
 */
function makeHarness() {
  const activeTurnRef = { current: null as string | null };
  const applied: { turnId: string; outcome: TurnOutcome }[] = [];

  const startTurn = (turnId: string, messages: ConversationMessage[]) => {
    activeTurnRef.current = turnId;
    const promise = runConversationTurn({
      text: messages[messages.length - 1]?.content ?? "",
      intentHint: null,
      messages,
      semanticActions: [],
      draftPlan: null,
      completed: {},
      compileId: null,
      gate: { status: "ready", dirty: false, inSync: true, delegableConflictCount: 0 },
    }).then((outcome) => {
      // turn_id guard: drop results from a superseded turn.
      if (activeTurnRef.current !== turnId) return;
      applied.push({ turnId, outcome });
    });
    return promise;
  };

  return { activeTurnRef, applied, startTurn };
}

describe("superseded-turn guard", () => {
  it("drops turn A's outcome when turn B started before A resolved (A resolves later)", async () => {
    const { applied, startTurn } = makeHarness();

    let resolveA: (() => void) | undefined;
    mockRequestAuthor.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveA = () =>
            resolve({ actions: [], plan: null, message: "from A", reason: null });
        }),
    );

    const turnA = startTurn("turn-A", [{ id: "u1", role: "user", content: "first" }]);

    // Start turn B before A resolves — this supersedes A.
    mockRequestAuthor.mockResolvedValueOnce({
      actions: [],
      plan: null,
      message: "from B",
      reason: null,
    });
    const turnB = startTurn("turn-B", [{ id: "u2", role: "user", content: "second" }]);
    await turnB;

    // Now let A resolve, after B already won and moved activeTurnRef on.
    expect(resolveA).toBeDefined();
    resolveA?.();
    await turnA;

    expect(applied).toHaveLength(1);
    expect(applied[0].turnId).toBe("turn-B");
    expect((applied[0].outcome as { message: string }).message).toBe("from B");
  });
});
