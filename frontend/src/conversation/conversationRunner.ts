/**
 * Phase 1 conversation runner: pure orchestration that decides intent, calls
 * the existing /author and /resolve_conflicts clients, and returns a
 * normalized TurnOutcome. Performs NO React state changes and reads NO React
 * state — everything it needs is passed in. This isolates the Phase 2 swap
 * point (NDJSON reader) behind a stable `runConversationTurn` contract.
 *
 * See docs/phase1_spec_unified_frontend_turn.md section 3.2.
 */
import { requestAuthor, applyRobotOverrides, type AugmentedAction, type ChatMessage } from "../plan/authorPlan";
import { requestConflictResolution } from "../plan/resolveConflicts";
import type { AuthoredPlan, AuthoredStep } from "../plan/planTypes";
import type { PlanEditDelta, ProtectedSet } from "../plan/planEdits";
import type { ConversationMessage, IntentHint, TurnOutcome } from "./conversationTypes";
import type { SceneContextRef } from "./sceneContext";
import type { PlanTaskRef } from "./planContext";

export type ResolveGate = {
  status: string;
  dirty: boolean;
  inSync: boolean;
  delegableConflictCount: number;
};

export type RunTurnArgs = {
  text: string;
  intentHint: IntentHint;
  messages: ConversationMessage[]; // full visible transcript incl. the new user msg
  semanticActions: AugmentedAction[];
  draftPlan: AuthoredPlan | null;
  /** compound_turn_integration_spec.md D1b: the current `livePlan` (the
   *  last committed turn_result's resolved plan), sent so the backend can
   *  graft this turn's newly appended actions onto it instead of re-solving
   *  every previously-fixed conflict from scratch. `null` when there is no
   *  prior resolved plan yet (first turn). */
  previousPlan?: AuthoredPlan | null;
  completed: Record<string, AuthoredStep[]>;
  compileId: string | null;
  gate: ResolveGate;
  /** Phase 4: threaded into the unified stream body's `plan_state` so the
   *  backend classifier/gate/Explain agent can see the same conflict/warning
   *  state the Gantt panel shows (see phase4 spec section 6.1). */
  conflicts?: unknown[];
  warnings?: unknown[];
  lastResolverReport?: unknown;
  /** Cursor-style scene references (picked objects/positions) attached to this
   *  turn. Serialized into the unified stream body as `scene_refs`. */
  sceneRefs?: SceneContextRef[];
  /** Per-turn handles ("p1", "p2", ...) for bindable position refs in
   *  `sceneRefs`, from `assignPinHandles`. Threaded into `serializeRefs` so
   *  the wire form's `handle` field matches the marker in the sent text. */
  sceneRefHandles?: Map<string, string>;
  /** Semantic task refs for the current message, serialized as `plan_refs`. */
  planRefs?: PlanTaskRef[];
  /** Per-message t1/t2 handles for `planRefs`. */
  planRefHandles?: Map<string, string>;
  signal?: AbortSignal;
  /** S3/D2 (compound_turn_integration_spec.md §5 item 16): the ordered
   *  manual-edit deltas active right now, sent with every turn so the
   *  backend can replay them onto its freshly decomposed authored plan.
   *  Defaults to empty (pre-S3 callers, and turns with no active edits). */
  edits?: PlanEditDelta[];
  /** Item 16 (D2, "one structure, two uses"): the protected set derived from
   *  `edits` via `buildProtectedSet`, threaded into the resolver's pin-veto
   *  matrix. Defaults to an empty set. */
  protected?: ProtectedSet;
};

/** Phase 1 deterministic routing: hint === "resolve" -> resolve, else author. */
export async function runConversationTurn(args: RunTurnArgs): Promise<TurnOutcome> {
  if (args.intentHint === "resolve") {
    const { status, dirty, inSync, delegableConflictCount } = args.gate;
    if (status !== "ready" || dirty || !inSync || delegableConflictCount === 0) {
      return {
        kind: "answer",
        intent: "resolve",
        content:
          "Can't resolve yet: need a compiled, in-sync plan with at least one " +
          "delegable conflict. Compile the current draft first.",
      };
    }
    const result = await requestConflictResolution(args.completed, args.compileId, {
      signal: args.signal,
    });
    return {
      kind: "resolve_result",
      plan: result.plan,
      compile: result.compile,
      message: result.message,
    };
  }

  // author: exclude non-author-tagged messages from model context (keep the
  // existing source !== "resolver" behavior, extended to "explain").
  const authorMessages: ChatMessage[] = args.messages
    .filter((m) => !m.excludeFromModel && m.source !== "resolver" && m.source !== "explain")
    .map((m) => ({ role: m.role, content: m.content, source: m.source as ChatMessage["source"] }));
  const currentActions = applyRobotOverrides(args.semanticActions, args.draftPlan);
  const result = await requestAuthor(authorMessages, currentActions, { signal: args.signal });
  return {
    kind: "author_result",
    actions: result.actions,
    plan: result.plan,
    message: result.message,
    reason: result.reason,
  };
}
