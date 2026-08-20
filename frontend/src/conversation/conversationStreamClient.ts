/**
 * NDJSON stream client. Phase 4 unifies author, resolve, and explain onto a
 * SINGLE request shape: every turn opens one POST /conversation/stream with a
 * unified body (turn_id, intent_hint, messages, current_actions, plan_state).
 * There is no more client-side author/resolve branching and no client-side
 * resolve eligibility gate — the backend classifies free text into
 * author/resolve/explain and gates resolve itself using the same
 * `plan_state` fields the client used to gate on.
 *
 * See docs/phase4_spec_classifier_explain_activities.md section 6.1.
 */
import type { AugmentedAction, ChatMessage } from "../plan/authorPlan";
import { applyRobotOverrides } from "../plan/authorPlan";
import type { AuthoredPlan, CompileResponse } from "../plan/planTypes";
import type { RunTurnArgs } from "./conversationRunner";
import type { Intent, TurnOutcome } from "./conversationTypes";
import { orchestratorUrl } from "../config";
import { serializeRefs } from "./sceneContext";
import { serializePlanRefs } from "./planContext";

export type AuthorResultArtifact = {
  kind: "author_result";
  actions: unknown[];
  plan: unknown;
  message: string;
  reason: string | null;
};

export type ResolveResultArtifact = {
  kind: "resolve_result";
  turn_id?: string;
  plan: unknown;
  compile?: unknown;
  report?: unknown;
  initial_snapshot_reused?: boolean;
};

/** compound_turn_integration_spec.md §3 — the compound tail's one terminal
 *  artifact (`stream_compound_turn`, backend, S1). `report`/`actions` are
 *  each present only when their stage actually ran this turn; `base_revision`
 *  is echoed back exactly as sent so a stale reply can be told apart from the
 *  current one (see `commitTurnResult` in `plan/turnState.ts`). */
export type TurnResultArtifact = {
  kind: "turn_result";
  turn_id?: string;
  plan: unknown;
  compile?: unknown;
  report?: unknown;
  actions?: unknown[];
  base_revision?: number;
  stages?: string[];
  deferred_pins?: unknown[];
  /** S3/D2: deltas from this turn's `edits` the backend could not replay. */
  dropped_edits?: unknown[];
  /** Deterministic summary of the manual deltas that actually replayed. */
  edit_message?: string;
  /** D8: split at the source (conversation.py's `stream_compound_turn`)
   *  rather than the frontend regex-splitting one composed string. See
   *  `TurnOutcome`'s `turn_result` variant for what each field is for. */
  author_message?: string;
  authoring_summary?: string;
  resolve_summary?: string;
  resolve_warning?: string;
};

/** Author verified that the requested semantic state already matches the
 * committed one.  The frontend keeps its current plan/compile artifact and
 * only completes the conversation message. */
export type NoChangeResultArtifact = {
  kind: "no_change_result";
  turn_id?: string;
  base_revision?: number;
  actions?: unknown[];
  stages?: string[];
  author_message?: string;
  authoring_summary?: string;
  reason?: string | null;
};

export type AnswerArtifact = {
  kind: "answer";
  turn_id?: string;
  content: string;
  intent?: string;
  follow_up?: boolean;
};

export type StreamEvent = {
  type:
    | "message_started"
    | "intent_selected"
    | "progress"
    | "warning"
    | "result"
    | "message_completed"
    | "error";
  turn_id?: string;
  seq?: number;
  intent?: string;
  stage?: string;
  text?: string;
  details?: unknown;
  artifact?:
    | AuthorResultArtifact
    | ResolveResultArtifact
    | TurnResultArtifact
    | NoChangeResultArtifact
    | AnswerArtifact;
};

async function readNdjsonStream(
  response: Response,
  onEvent: (event: StreamEvent) => void,
  failureLabel: string,
): Promise<StreamEvent["artifact"] | null> {
  if (!response.body) {
    throw new Error(`${failureLabel}: response had no body`);
  }

  let artifact: StreamEvent["artifact"] | null = null;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const handleLine = (line: string) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const event = JSON.parse(trimmed) as StreamEvent;
    if (event.type === "error") {
      const error = new Error(event.text ?? failureLabel) as Error & { details?: unknown };
      error.details = event.details;
      throw error;
    }
    if (event.type === "result" && event.artifact) {
      artifact = event.artifact;
    }
    onEvent(event);
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newlineIndex: number;
      while ((newlineIndex = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newlineIndex);
        buffer = buffer.slice(newlineIndex + 1);
        handleLine(line);
      }
    }
    // flush any trailing partial line left in the decoder/buffer.
    buffer += decoder.decode();
    if (buffer.trim()) {
      handleLine(buffer);
    }
  } finally {
    reader.releaseLock();
  }

  return artifact;
}

/** Every turn (author, resolve, explain) routes through
 *  POST /conversation/stream with one unified body; the backend classifies
 *  free text and gates resolve eligibility. onEvent fires for every stream
 *  event (used to update the live message + process trail). Resolves with
 *  the terminal TurnOutcome, dispatched by the artifact's `kind`. */
export async function streamConversationTurn(
  args: RunTurnArgs & {
    turnId: string;
    /** compound_turn_integration_spec.md §3: monotone, owned by ScenePage,
     *  bumped on every commit and every local edit. Sent with every turn so
     *  the backend can echo it in the `turn_result` artifact (when it does)
     *  and a superseded reply can be discarded rather than applied. */
    baseRevision: number;
  },
  onEvent: (event: StreamEvent) => void,
): Promise<TurnOutcome> {
  // Same context filter as runConversationTurn/Phase 1-3: exclude
  // resolver/explain-tagged messages from Author's (and now the shared
  // unified) context.
  const authorMessages: ChatMessage[] = args.messages
    .filter((m) => !m.excludeFromModel && m.source !== "resolver" && m.source !== "explain")
    .map((m) => ({ role: m.role, content: m.content, source: m.source as ChatMessage["source"] }));
  const currentActions = applyRobotOverrides(args.semanticActions, args.draftPlan);

  const planState = {
    status: args.gate.status,
    dirty: args.gate.dirty,
    in_sync: args.gate.inSync,
    delegable_conflict_count: args.gate.delegableConflictCount,
    completed_plan: args.completed,
    compile_id: args.compileId,
    draft_plan: args.draftPlan,
    conflicts: args.conflicts ?? [],
    warnings: args.warnings ?? [],
    last_resolver_report: args.lastResolverReport ?? null,
  };

  let response: Response;
  try {
    response = await fetch(`${orchestratorUrl}/conversation/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
      body: JSON.stringify({
        turn_id: args.turnId,
        intent_hint: args.intentHint,
        messages: authorMessages,
        current_actions: currentActions,
        plan_state: planState,
        scene_refs: serializeRefs(args.sceneRefs ?? [], args.sceneRefHandles),
        plan_refs: serializePlanRefs(args.planRefs ?? [], args.planRefHandles),
        // D1b: the frontend's `livePlan`, so the backend can promote a pure
        // append onto it (decompose.merge_appended_tasks) instead of paying
        // to re-resolve conflicts this turn's plan already had fixed.
        previous_plan: args.previousPlan ?? null,
        base_revision: args.baseRevision,
        // S3 (compound_turn_integration_spec.md §5 item 16): the accumulated
        // manual-edit deltas, replayed server-side onto the freshly
        // decomposed authored plan (D2), plus the protected set derived from
        // them (threaded into the resolver's pin-veto matrix). Both default
        // to empty for every pre-S3 caller.
        edits: args.edits ?? [],
        protected: args.protected ?? {},
      }),
      signal: args.signal,
    });
  } catch (err) {
    throw new Error(
      `conversation stream request failed (is the orchestrator service running at ${orchestratorUrl}?): ${
        err instanceof Error ? err.message : String(err)
      }`,
    );
  }

  if (!response.ok) {
    throw new Error(`conversation stream failed: ${response.status}`);
  }

  let lastMessageCompletedText: string | undefined;
  const artifact = await readNdjsonStream(
    response,
    (event) => {
      if (event.type === "message_completed") lastMessageCompletedText = event.text;
      onEvent(event);
    },
    "conversation stream failed",
  );
  if (!artifact) {
    throw new Error("conversation stream ended without a result event");
  }

  switch (artifact.kind) {
    case "turn_result": {
      const a = artifact as TurnResultArtifact;
      return {
        kind: "turn_result",
        plan: (a.plan ?? null) as AuthoredPlan | null,
        compile: a.compile as CompileResponse | undefined,
        report: a.report,
        actions: a.actions as AugmentedAction[] | undefined,
        baseRevision: a.base_revision,
        droppedEdits: a.dropped_edits,
        editMessage: a.edit_message,
        authorMessage: a.author_message,
        authoringSummary: a.authoring_summary,
        resolveSummary: a.resolve_summary,
        resolveWarning: a.resolve_warning,
        message: lastMessageCompletedText ?? "Plan updated.",
      };
    }
    case "no_change_result": {
      const a = artifact as NoChangeResultArtifact;
      return {
        kind: "no_change_result",
        baseRevision: a.base_revision,
        authorMessage: a.author_message,
        authoringSummary: a.authoring_summary,
        reason: a.reason ?? null,
        message: lastMessageCompletedText ?? a.author_message ?? "No plan changes were needed.",
      };
    }
    case "answer": {
      const a = artifact as AnswerArtifact;
      return {
        kind: "answer",
        intent: (a.intent as Intent | undefined) ?? "explain",
        content: a.content,
      };
    }
    case "author_result":
    case "resolve_result":
      // compound_turn_integration_spec.md §9 (S2 rollback note): once the
      // frontend converges on turn_result (item 15), it can no longer serve
      // either legacy artifact -- both require the draft + Compile review
      // gate this slice deletes. Seeing one here means the backend is not
      // running with COMPOUND_TURN=1; that is an actionable deployment
      // mismatch, not a generic "unknown kind".
      throw new Error(
        `conversation stream returned a legacy "${artifact.kind}" artifact -- ` +
          "backend is not running COMPOUND_TURN=1 (this frontend build requires it).",
      );
    default:
      throw new Error(`conversation stream returned an unknown artifact kind: ${String(
        (artifact as { kind?: unknown }).kind,
      )}`);
  }
}
