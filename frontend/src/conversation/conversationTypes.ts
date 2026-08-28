/**
 * Phase 1 unified conversation types (frontend-only, non-streaming).
 *
 * See docs/phase1_spec_unified_frontend_turn.md section 3.1. This is a
 * superset of the old `ChatMessage` (plan/authorPlan.ts): `role`/`content`/
 * `source` are preserved so the existing `source !== "resolver"` filtering
 * keeps working, extended with a `status` (turn lifecycle) and `intent` tag.
 */
import type { AugmentedAction } from "../plan/authorPlan";
import type { AuthoredPlan, CompileResponse } from "../plan/planTypes";

export type Intent = "author" | "resolve" | "explain" | "clarify";
// "edit" (S3 item 17, revised): the sync button's pending-edit-batch branch.
// Structured, not natural language -- same reasoning as "resolve"/the
// sync button's no-pending-edits branch (D3), so it skips the author stage
// the same way. No longer auto-fired by a debounce: it only reaches the
// backend when the user presses sync with edits staged.
export type IntentHint = "resolve" | "edit" | null;
export type MessageStatus = "working" | "done" | "error";

export type UserMessageDisplayPart =
  | { type: "text"; value: string }
  | {
      type: "ref";
      label: string;
      kind: "object" | "facility" | "robot" | "position" | "plan_task";
      /** Robot identity for plan-task chips; absent on legacy transcript data. */
      robot?: string;
    };

/** Unified message model. Superset of the old ChatMessage (role/content/source
 *  preserved) so existing filtering keeps working. */
export type ConversationMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  /** Frontend-only rich representation of a user message. The backend still
   * receives `content`; these parts only preserve the composer's reference
   * chips in the visible transcript and version snapshots. */
  displayParts?: UserMessageDisplayPart[];
  /** assistant only; user messages have no status */
  status?: MessageStatus;
  intent?: Intent;
  /** producer tag; used to exclude non-author content from Author context */
  source?: "authoring" | "resolver" | "explain";
  /** Visible transcript content that must never be sent back to the model. */
  excludeFromModel?: boolean;
  /** Phase 2: accumulated stream activity log (intent_selected/progress/warning
   *  events), ordered by seq. LIVE-only past Phase 4/D8: this is the
   *  per-attempt trail that justifies itself while a turn is running (the
   *  latest entry is what the working-state headline shows) but is mechanism
   *  noise once the turn ends -- AssistantMessage stops rendering it as soon
   *  as `status` leaves "working", even though the array itself is left
   *  populated here (simplest correct thing: no separate "done, so truncate"
   *  write path to keep in sync with the render gate). */
  activities?: { seq: number; stage?: string; text: string }[];
  /** D8: the resolve summary's accountability half (numbered adjustments +
   *  compound-turn time) for a converged/partially-converged turn_result --
   *  rendered behind "See details" once the turn is done. Absent (no
   *  toggle) when resolve did not run or ran with nothing to itemize. Never
   *  the unresolved-conflicts warning -- that rides in `content` instead
   *  (D8: it must never be behind the toggle). */
  details?: string;
};

/** Normalized result of running one turn. Only *_result kinds mutate plan state.
 *
 * `author_result`/`resolve_result` are the pre-compound-turn shapes. They are
 * kept here only for `conversationRunner.ts` (the Phase 1 non-streaming path,
 * untouched by the compound-turn slices) — `conversationStreamClient.ts`'s
 * compound path never produces them: per compound_turn_integration_spec.md §9
 * (S2), a stream that returns either as an artifact means the backend isn't
 * running `COMPOUND_TURN=1`, which the stream client rejects outright rather
 * than mapping to one of these. */
export type TurnOutcome =
  | {
      kind: "author_result";
      actions: AugmentedAction[];
      plan: AuthoredPlan | null;
      message: string;
      reason: string | null;
    }
  | {
      kind: "resolve_result";
      plan: AuthoredPlan;
      compile?: CompileResponse;
      /** Phase 4: last resolver report, retained by ScenePage for Explain
       *  context (docs/phase4_spec_classifier_explain_activities.md section 6.2). */
      report?: unknown;
      message: string;
    }
  | {
      kind: "turn_result";
      /** D1's corollary: the frontend never holds the authored plan — this
       *  IS the only user-visible plan (ScenePage's `livePlan`), regenerated
       *  server-side every turn. `null` only in the no-semantic-actions
       *  degenerate case (nothing to compile/resolve this turn). */
      plan: AuthoredPlan | null;
      compile?: CompileResponse;
      /** Present only when resolve ran this turn. */
      report?: unknown;
      /** Present only when the author stage ran this turn (absent for a
       *  sync/edit-triggered tail run). */
      actions?: AugmentedAction[];
      /** Echoed back by the backend when it threads `base_revision` through;
       *  the caller MUST discard (not apply) an artifact whose value here
       *  disagrees with the current one (integration spec §3). */
      baseRevision?: number;
      /** D2b: deltas from this turn's `edits` the backend could not apply
       *  (their target did not exist in the plan they were applied to, or —
       *  a non-pure-append author turn — the whole batch was superseded by a
       *  restructure). Purely diagnostic on the frontend: every delta sent
       *  with a turn is cleared from the pending list on a successful commit
       *  regardless of whether it landed here (see `commitTurnResult`'s
       *  `sentEdits`), since a delta that could not apply this turn has
       *  nothing left to retry against next turn either. The stream's own
       *  `warning` event (rendered as an activity line) is what actually
       *  tells the user which edit was lost -- this field exists so a test
       *  can assert on it without parsing chat text. */
      droppedEdits?: unknown[];
      /** Deterministic user-facing summary for a Sync/manual-edit turn. */
      editMessage?: string;
      /** D8: the terminal artifact's separately-sent presentation fields,
       *  rather than one composed `message` string for the frontend to pull
       *  apart. `authorMessage` is the LLM's own text (absent on a sync/edit
       *  tail run, which skips the author stage entirely); `resolveWarning`
       *  is the non-converged / pin-deferral sentence(s), which MUST land in
       *  the chat bubble (never behind "See details" -- that is precisely
       *  the sentence a user must not have to go looking for);
       *  `resolveSummary` is the numbered adjustment list + compound-turn time,
       *  which is accountability detail useless while nothing has happened
       *  yet and belongs behind the toggle once the turn is done. `message`
       *  is kept as the `message_completed` fallback (see
       *  conversationStreamClient's mapping) for any caller still reading it
       *  as one string. */
      authorMessage?: string;
      /** Deterministic author-tool audit: augment output ids/allocations and
       *  every completed reassign/update operation. */
      authoringSummary?: string;
      resolveSummary?: string;
      resolveWarning?: string;
      message: string;
    }
  | {
      /** Author returned the same semantic state and the backend deliberately
       * skipped decompose adoption, Compile, Resolve, and plan versioning. */
      kind: "no_change_result";
      baseRevision?: number;
      authorMessage?: string;
      authoringSummary?: string;
      reason: string | null;
      message: string;
    }
  | { kind: "answer"; content: string; intent: Intent } // non-mutating (precondition/explain)
  | { kind: "error"; message: string };
