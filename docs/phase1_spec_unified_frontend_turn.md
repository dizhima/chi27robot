# Phase 1 Spec — Unified Frontend Turn State (non-streaming)

> Status: implementation spec, ready to execute. Frontend only. No backend changes.
>
> Parent contracts: `phase0_unified_conversation_contracts.md`.
> Parent design: `unified_conversation_author_resolver_design.md` (§14 Phase 1).
>
> Executor note: this spec is self-contained. Do not infer requirements from any
> other conversation. Everything you need is here plus the two docs above and the
> files named in section 2.

## 1. Goal and scope

Replace the two separate chat/resolve code paths in the frontend with **one
conversation entry point and one turn lifecycle**, WITHOUT changing transport or
the backend. The existing `POST /author` and `POST /resolve_conflicts` endpoints
are still called; Phase 1 only unifies the frontend state machine so that:

- there is a single `sendConversation(text, intentHint?)` entry;
- a single "active turn" state replaces `chatPending` + `resolvePending`;
- every turn creates exactly one assistant message and updates it in place
  (`working` -> `done` | `error`);
- only a terminal outcome mutates plan state;
- a `turn_id` guard drops results from a superseded turn;
- the Resolve button becomes a shortcut that passes `intentHint: "resolve"`.

### In scope

- Frontend turn state, message model, one entry function, Resolve-as-shortcut.
- A thin `conversationRunner` that does intent routing + calls existing clients
  and returns a normalized outcome. This is the seam Phase 2 replaces with an
  NDJSON reader.

### Explicitly OUT of scope (do NOT build in Phase 1)

- No `POST /conversation/stream`, no NDJSON, no streaming. (Phase 2.)
- No LLM intent classifier. Phase 1 routing is deterministic (see section 4).
- No Explain/clarify workflow. (Phase 4.) The only non-mutating outcome in
  Phase 1 is a resolve precondition message.
- No `plan_state.revision`. The guard is `turn_id` only.
- No changes to `authorPlan.ts` / `resolveConflicts.ts` request/response shapes,
  and no changes to any `.py` file.
- No changes to compile behavior, `usePlanCompile`, `adoptCompileResult`,
  `stagePlan`, `stageSeededPlan`, `ensureStepIds`, or `applyRobotOverrides`.

## 2. Current code you are modifying (verified facts)

All paths under `frontend/src/`.

`ScenePage.tsx` — the only component to modify. Relevant current symbols:

- State (lines ~58-76): `draftPlan`, `committedPlan`, `messages: ChatMessage[]`,
  `semanticActions: AugmentedAction[]`, `chatDraft`, `chatPending`,
  `resolvePending`, `chatError`.
- Derived (lines ~63-68): `dirty = draftPlan !== committedPlan`,
  `inSync = committedPlan === compiledPlan`, plus `status`, `completed`,
  `compileId`, `adoptCompileResult` from `usePlanCompile(committedPlan)`.
- `delegableConflictCount` — a derived count already computed later in the file
  (used by the Resolve button disabled condition, lines ~609-614).
- `stageSeededPlan(seed, {compile})` (line 247) and `stagePlan(plan, {compile})`
  (line 257): stage a draft; `compile:true` also sets `committedPlan`.
- `sendChat(text)` (line 277): current author path. Filters
  `messages.filter(m => m.source !== "resolver")`, calls `requestAuthor`, on
  success `setSemanticActions`, `stagePlan(result.plan, {compile:false})`,
  appends an assistant message.
- `resolve()` (line 319): current resolve path. Gate is
  `status === "ready" && !dirty && inSync && delegableConflictCount > 0`. Calls
  `requestConflictResolution(completed, compileId)`; on success adopts
  `result.compile` via `adoptCompileResult` then `stageSeededPlan(seed,{compile:true})`
  (or plain `stageSeededPlan` for V1 no-compile), appends a resolver-tagged
  assistant message.
- Render: status chip (line 512), message list (lines 531-540) rendering
  `message.role` + `message.content`, composer (lines 547-575) disabled on
  `chatPending || resolvePending`, Resolve/Compile/Clear buttons (lines 593-633).

`plan/authorPlan.ts` — DO NOT MODIFY. Provides:
- `ChatMessage = { role: "user"|"assistant"; content: string; source?: "authoring"|"resolver" }`
- `requestAuthor(messages: ChatMessage[], currentActions, opts?) => AuthorResponse`
  where `AuthorResponse = { actions, plan: AuthoredPlan|null, message, reason }`.
- `applyRobotOverrides(actions, plan) => AugmentedAction[]`.

`plan/resolveConflicts.ts` — DO NOT MODIFY. Provides:
- `requestConflictResolution(completedPlan, compileId, opts?) => ResolveConflictsResponse`
  where `ResolveConflictsResponse = { plan, report, message, initial_snapshot_reused?, compile? }`.

## 3. New files

### 3.1 `frontend/src/conversation/conversationTypes.ts`

```ts
export type Intent = "author" | "resolve" | "explain" | "clarify";
export type IntentHint = "resolve" | null;
export type MessageStatus = "working" | "done" | "error";

/** Unified message model. Superset of the old ChatMessage (role/content/source
 *  preserved) so existing filtering keeps working. */
export type ConversationMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  /** assistant only; user messages have no status */
  status?: MessageStatus;
  intent?: Intent;
  /** producer tag; used to exclude non-author content from Author context */
  source?: "authoring" | "resolver" | "explain";
};

/** Normalized result of running one turn. Only *_result kinds mutate plan state. */
export type TurnOutcome =
  | { kind: "author_result"; actions: import("../plan/authorPlan").AugmentedAction[];
      plan: import("../plan/planTypes").AuthoredPlan | null; message: string; reason: string | null }
  | { kind: "resolve_result"; plan: import("../plan/planTypes").AuthoredPlan;
      compile?: import("../plan/planTypes").CompileResponse; message: string }
  | { kind: "answer"; content: string; intent: Intent }   // non-mutating (precondition/explain)
  | { kind: "error"; message: string };
```

### 3.2 `frontend/src/conversation/conversationRunner.ts`

A pure orchestration function: it decides intent, calls the existing client, and
returns a `TurnOutcome`. It performs NO React state changes and reads NO React
state — everything it needs is passed in. This isolates the Phase 2 swap point.

```ts
import { requestAuthor, applyRobotOverrides, type AugmentedAction, type ChatMessage }
  from "../plan/authorPlan";
import { requestConflictResolution } from "../plan/resolveConflicts";
import type { AuthoredPlan, AuthoredStep } from "../plan/planTypes";
import type { ConversationMessage, IntentHint, TurnOutcome } from "./conversationTypes";

export type ResolveGate = {
  status: string;
  dirty: boolean;
  inSync: boolean;
  delegableConflictCount: number;
};

export type RunTurnArgs = {
  text: string;
  intentHint: IntentHint;
  messages: ConversationMessage[];       // full visible transcript incl. the new user msg
  semanticActions: AugmentedAction[];
  draftPlan: AuthoredPlan | null;
  completed: Record<string, AuthoredStep[]>;
  compileId: string | null;
  gate: ResolveGate;
  signal?: AbortSignal;
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
    return { kind: "resolve_result", plan: result.plan, compile: result.compile,
             message: result.message };
  }

  // author: exclude non-author-tagged messages from model context (keep the
  // existing source !== "resolver" behavior, extended to "explain").
  const authorMessages: ChatMessage[] = args.messages
    .filter((m) => m.source !== "resolver" && m.source !== "explain")
    .map((m) => ({ role: m.role, content: m.content, source: m.source as ChatMessage["source"] }));
  const currentActions = applyRobotOverrides(args.semanticActions, args.draftPlan);
  const result = await requestAuthor(authorMessages, currentActions, { signal: args.signal });
  return { kind: "author_result", actions: result.actions, plan: result.plan,
           message: result.message, reason: result.reason };
}
```

## 4. Routing (Phase 1, deterministic)

Exactly one rule: `intentHint === "resolve"` routes to Resolver; everything else
routes to Author. No classifier, no explain/clarify. This is intentional — it
validates the turn machinery, not language understanding. Phase 2 replaces the
runner body with the classifier + streaming; the ScenePage-facing contract
(`sendConversation`, `TurnOutcome`) stays.

## 5. ScenePage.tsx changes

### 5.1 State

- Replace `const [messages, setMessages] = useState<ChatMessage[]>([])` with
  `useState<ConversationMessage[]>([])`.
- Remove `chatPending` and `resolvePending`. Add:
  ```ts
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const activeTurnRef = useRef<string | null>(null);
  ```
  Derive `const turnBusy = activeTurnId !== null;`
- Keep `chatError`, `chatDraft`, `semanticActions` as-is.
- Update the imports: drop `ChatMessage` type usage in state (still import it in
  the runner), import `ConversationMessage`, `IntentHint`, `TurnOutcome` from
  `./conversation/conversationTypes` and `runConversationTurn` from
  `./conversation/conversationRunner`.

### 5.2 Entry function — replace `sendChat` and `resolve`

Add one function; delete `sendChat` and `resolve`.

```ts
const sendConversation = (text: string, intentHint: IntentHint = null) => {
  const content = text.trim();
  if (!content || turnBusy) return;
  const turnId = crypto.randomUUID();
  const userMsg: ConversationMessage = { id: crypto.randomUUID(), role: "user", content };
  const assistantId = crypto.randomUUID();
  const placeholder: ConversationMessage = {
    id: assistantId, role: "assistant", status: "working",
    intent: intentHint === "resolve" ? "resolve" : "author",
    content: intentHint === "resolve" ? "Resolving…" : "Authoring…",
  };
  const nextMessages = [...messages, userMsg];
  followLatestChatRef.current = true;
  setMessages([...nextMessages, placeholder]);
  if (intentHint !== "resolve") setChatDraft("");
  setChatError(null);
  setActiveTurnId(turnId);
  activeTurnRef.current = turnId;
  setSchedulePlaying(false);

  runConversationTurn({
    text: content,
    intentHint,
    messages: nextMessages,
    semanticActions,
    draftPlan,
    completed,
    compileId,
    gate: { status, dirty, inSync, delegableConflictCount },
  })
    .then(async (outcome) => {
      // turn_id guard: drop results from a superseded turn.
      if (activeTurnRef.current !== turnId) return;
      await applyOutcome(assistantId, outcome);
    })
    .catch((err) => {
      if (activeTurnRef.current !== turnId) return;
      const msg = err instanceof Error ? err.message : String(err);
      finalizeMessage(assistantId, { status: "error", content: msg });
      setChatError(msg);
    })
    .finally(() => {
      if (activeTurnRef.current === turnId) {
        activeTurnRef.current = null;
        setActiveTurnId(null);
      }
    });
};
```

Helper to update one message in place by id:

```ts
const finalizeMessage = (id: string, patch: Partial<ConversationMessage>) =>
  setMessages((cur) => cur.map((m) => (m.id === id ? { ...m, ...patch } : m)));
```

Terminal artifact application — the ONLY place plan state is mutated:

```ts
const applyOutcome = async (assistantId: string, outcome: TurnOutcome) => {
  if (outcome.kind === "author_result") {
    setSemanticActions(outcome.actions);
    if (outcome.plan) stagePlan(outcome.plan, { compile: false });   // draft, no auto-compile
    finalizeMessage(assistantId, {
      status: "done", source: "authoring",
      content: outcome.message || outcome.reason || "Plan updated.",
    });
  } else if (outcome.kind === "resolve_result") {
    const seed = ensureStepIds(outcome.plan);
    if (outcome.compile) {
      await adoptCompileResult(seed, outcome.compile);              // no /compile_plan
      stageSeededPlan(seed, { compile: true });
    } else {
      stageSeededPlan(seed, { compile: true });                    // V1 rollback path
    }
    finalizeMessage(assistantId, { status: "done", source: "resolver", content: outcome.message });
  } else if (outcome.kind === "answer") {
    finalizeMessage(assistantId, { status: "done", source: "explain", content: outcome.content });
  } else {
    finalizeMessage(assistantId, { status: "error", content: outcome.message });
    setChatError(outcome.message);
  }
};
```

Note: `applyOutcome`'s author/resolve branches must reproduce the exact staging
calls the current `sendChat`/`resolve` use — same `{compile:false}` for author,
same `adoptCompileResult` + `stageSeededPlan({compile:true})` for resolver. Do
not change staging semantics.

### 5.3 Render

- Status chip (line ~512): show `activeTurnId ? (intent==="resolve" ? "Resolving…" : "Authoring…") : "Ready"`. Simplest correct form: derive from the last
  assistant message's `status`/`intent`, or keep a small `turnLabel`. Any
  equivalent is fine; it must read "Ready" when idle.
- Message list (lines ~531-540): key by `message.id` (not index). When
  `message.status === "working"`, render a busy affordance (e.g. append " …" or a
  spinner class). `done`/`error`/undefined render normally. `error` may use the
  existing `chat-rail-error` styling.
- Composer (lines ~554-574): change `disabled={chatPending || resolvePending}` to
  `disabled={turnBusy}`; submit calls `sendConversation(chatDraft)`.
- Resolve button (lines ~605-623): `onClick={() => sendConversation("Resolve current conflicts", "resolve")}`.
  Keep its existing `disabled` condition (status/dirty/inSync/count) as a UX
  guard, and change `resolvePending` in it to `turnBusy`. Label: `turnBusy ? "Resolving…" : "Resolve"` is acceptable, or keep "Resolve".
- Compile button and Clear button: replace any `resolvePending` disabled ref with
  `turnBusy`. Clear (`clearPlan`) must also `setActiveTurnId(null)` /
  `activeTurnRef.current = null` is not needed (Clear is disabled while busy), but
  ensure Clear's disabled uses `turnBusy`.
- `clearPlan` (line ~263) sets `setMessages([])` — keep; type is now
  `ConversationMessage[]`, empty array is fine.

### 5.4 Editing disabled while a turn runs (decision Q6)

Plan editing must be disabled while `turnBusy`. Locate the plan-edit entry points
(`editDraft` at line ~353 and the inline editor / marker drag handlers) and guard
them with `if (turnBusy) return;` or disable their controls. Minimum acceptable:
`editDraft` early-returns when `turnBusy`. Note this in the PR description so the
reviewer can confirm coverage.

## 6. What must NOT change (guardrails)

1. No `.py` changes. No changes to `authorPlan.ts` or `resolveConflicts.ts`.
2. Author result still stages a draft with `{compile:false}` (no auto-compile).
3. Resolver result still adopts `result.compile` via `adoptCompileResult` and
   does NOT call `/compile_plan`; V1 (no `compile`) still recompiles.
4. Author context still excludes resolver-tagged (and now explain-tagged)
   messages before `requestAuthor`.
5. `applyRobotOverrides(semanticActions, draftPlan)` is still applied before the
   author call.
6. The Resolve eligibility gate (status ready, not dirty, in sync, delegable
   count > 0) still holds; a failing resolve intent produces a non-mutating
   message and does not call the backend.

## 7. Acceptance tests

Detect the frontend test runner first (check `frontend/package.json` for
`vitest`/`jest` and any `*.test.ts(x)` examples) and match its conventions. Put
tests next to the code under `frontend/src/conversation/`.

Unit tests for `runConversationTurn` (mock the two clients):

1. `intentHint: "resolve"` with a passing gate calls `requestConflictResolution`
   and returns `{ kind: "resolve_result" }` carrying the mock `compile`.
2. `intentHint: "resolve"` with `dirty: true` (or `delegableConflictCount: 0`, or
   `inSync: false`, or `status: "compiling"`) returns `{ kind: "answer" }` and
   does NOT call `requestConflictResolution`.
3. Default (no hint) routes to `requestAuthor` and returns `{ kind: "author_result" }`.
4. Author routing excludes messages tagged `source: "resolver"` and
   `source: "explain"` from the array passed to `requestAuthor`.
5. A client rejection propagates so the caller sees a thrown error (the runner
   does not swallow it) — verify `requestAuthor` rejection bubbles out of
   `runConversationTurn`.

If a component test harness exists, add:

6. A superseded-turn guard test: start turn A, start turn B before A resolves;
   when A resolves, the plan reflects B only (A's outcome is dropped). If a full
   component harness is impractical, instead unit-test the guard by asserting the
   `activeTurnRef` comparison logic in a small extracted helper. Document which
   approach you took.

## 8. Definition of done

- `sendChat` and `resolve` are gone; `sendConversation` is the only entry.
- `chatPending`/`resolvePending` are gone; one `activeTurnId` drives busy state.
- One assistant message per turn, updated in place to `done`/`error`.
- Resolve button routes through `sendConversation(_, "resolve")`.
- Plan editing is disabled while a turn runs.
- All new unit tests pass; `npm run build` (or the project's typecheck) passes
  with no new TypeScript errors.
- No backend files changed; no changes to the two client files.

## 9. Report back

In your final report include: the exact list of files changed, how you handled
the superseded-turn test (component vs extracted helper), where you disabled plan
editing, and the test command + its output summary.
