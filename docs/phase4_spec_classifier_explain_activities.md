# Phase 4 Spec — Intent Classifier + Explain Agent + Process Trail

> Status: implementation spec, ready to execute. The largest phase. Adds backend
> intent routing for free-text, a read-only Explain workflow, and the collapsible
> activity trail UI.
>
> Parent contracts: `phase0_unified_conversation_contracts.md`.
> Parent design: `unified_conversation_author_resolver_design.md` (§14 Phase 4).
> Prerequisites: Phases 1–3 implemented (author + resolve stream through
> `POST /conversation/stream`; `conversation.py` has `stream_author_turn` and
> `stream_resolve_turn`; frontend `streamConversationTurn` + `ConversationMessage.activities[]`).
>
> Executor note: self-contained. Do not infer requirements elsewhere.

## 0. Locked decisions (confirmed with the user)

1. **Intents merge to THREE: `author`, `resolve`, `explain`.** `clarify` is
   removed as a separate intent. Rationale: an ambiguous *mutating* request is
   already handled by Author itself returning `plan=null` + an explanatory
   `message/reason`; a pure *question* is `explain`. A read-only answer may set an
   optional `follow_up` flag when it asks the user something back. Reintroduce a
   distinct `clarify` only if/when cross-turn "suspended mutation" resumption is
   built. This supersedes the 4-intent table in phase0 §6.
2. **Routing = validated UI hint, else a single LLM classifier.** No deterministic
   phrase engine (the user chose "just the classifier"). The classifier returns
   one of the three intents.
3. **Explain is one read-only LLM agent** with FOUR read-only tools
   (`read_author_transcript`, `read_resolver_report`, `read_plan`,
   `read_conflicts`) plus a terminal `respond`. No mutation tools of any kind.
4. **Process trail UI: default collapsed**, covering author/resolve/explain
   assistant messages.

## 1. Scope

### In scope

- Backend: `classify_intent` (typed, `force_tool`); unified routing in
  `_handle_conversation_stream`; move the resolve eligibility gate to the backend
  for classifier-routed resolve; the read-only Explain agent + `stream_explain_turn`.
- Frontend: unified request body; simplify `streamConversationTurn` to always
  stream and dispatch by terminal-artifact `kind`; retain the last resolver report
  for Explain context; collapsible activity trail (default collapsed).

### OUT of scope

- No cross-turn suspended-mutation resumption (no revived `clarify`).
- No change to Resolver V2 scheduler/budgets/`compile_count`, Author tools, or
  compile semantics.
- No `plan_state.revision` (turn_id guard only).
- Keep `POST /author` and `POST /resolve_conflicts` as-is (compat/rollback).

## 2. Current code (verified facts)

- `loop.run(user_msg, provider, tools, execute, *, history, max_iters,
  terminal_tools, on_event)` (loop.py) terminates and returns the message history
  when the model replies with NO tool calls (final `content` is the answer), or
  when a `terminal_tools` member is called. Events: `assistant_text`,
  `tool_call`, `tool_result`, `max_iters`.
- `LLMProvider` (providers/base.py) has `chat(messages, tools)` and
  `force_tool(messages, tools, tool_name)` — the latter forces one named
  structured tool call (used by `authoring.author`'s recovery path). Read
  `reply.tool_calls[0].arguments` for the forced result.
- `ToolSpec(name, description, parameters)` and `Message`, `ToolCall` live in
  `schema.py`. `AUGMENT_INPUT_SCHEMA` etc. show the JSON-schema style to mirror.
- `conversation.py` (Phases 2–3): `stream_author_turn(body, write_event, *,
  provider, manifest)`, `stream_resolve_turn(body, write_event, *, provider,
  compile_fn, snapshot_lookup_fn)`, `resolver_progress_text`, `AUTHOR_PROGRESS`,
  and the envelope pattern (`message_started`, `intent_selected`, `progress`,
  `warning`, `result`, `message_completed`, `error`).
- `service.py` `_handle_conversation_stream` (Phase 2/3): opens the NDJSON stream,
  builds `stamped_write_event` (adds `seq`+`turn_id`), then branches on
  `intent_hint == "resolve"` (→ `stream_resolve_turn`) else `stream_author_turn`.
- Frontend `conversation/conversationStreamClient.ts`: `streamConversationTurn`
  branches author vs resolve, resolve gate is CLIENT-SIDE, `readNdjsonStream`
  shared helper, terminal artifact → `TurnOutcome` by kind. `ScenePage.applyOutcome`
  already handles `author_result`, `resolve_result`, and `answer` (answer →
  `finalizeMessage(status:"done", source:"explain", content)`). `ConversationMessage`
  has `activities?: {seq; stage?; text}[]`; `onEvent` appends them and sets the
  headline. Render (ScenePage ~572-587): user → bubble; assistant → `.chat-assistant`
  flowing text with `.think-dot` while working.

## 3. Part A — Intent classifier (backend)

Add to `conversation.py`:

```python
INTENTS = ("author", "resolve", "explain")

CLASSIFY_TOOL = ToolSpec(
    name="classify",
    description="Classify the user's latest message into exactly one workflow.",
    parameters={
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": list(INTENTS)},
            "confidence": {"type": "number"},
            "reason_code": {"type": "string"},
        },
        "required": ["intent", "confidence", "reason_code"],
        "additionalProperties": False,
    },
)

def classify_intent(latest_user_text, state_hint, provider) -> dict:
    """One forced structured call -> {intent, confidence, reason_code}."""
```

- `state_hint`: a compact dict, e.g. `{"plan_exists": bool, "delegable_conflict_count": int}`,
  so the model can separate "resolve the conflicts" from "why is there a conflict".
- Prompt guidance (in the system message): author = create/modify tasks (add/
  remove/move objects, change destination/robot); resolve = fix scheduling
  conflicts / avoid collision / improve ordering; explain = a read-only question
  about the plan, schedule, ordering, remaining conflicts, or status. Explicit
  semantic edits win over the mere presence of conflicts.
- Return the validated arguments dict; if the model returns an invalid intent,
  default to `author` with `reason_code="classifier_fallback"` (author is the safe
  non-destructive default — it stages a draft the user reviews).
- `reason_code` is telemetry/test only; never shown to the user.

## 4. Part B — Explain agent (backend)

### 4.1 Read-only tools

Add to `conversation.py`. Each tool takes NO input args (or ignores them) and
returns data drawn from the request `body` (captured by the executor closure).
NONE mutate anything.

| tool | returns |
|---|---|
| `read_author_transcript` | the author-tagged transcript: `body.messages` filtered to `source in (None,"authoring")` and `role in ("user","assistant")` |
| `read_resolver_report` | `body.plan_state.last_resolver_report` or `{"available": false}` when absent |
| `read_plan` | `{"draft": body.plan_state.draft_plan, "completed": body.plan_state.completed_plan}` |
| `read_conflicts` | `{"conflicts": body.plan_state.conflicts, "warnings": body.plan_state.warnings, "status": ..., "in_sync": ..., "delegable_conflict_count": ...}` |

Plus a terminal tool:

```python
RESPOND_TOOL = ToolSpec(
    name="respond",
    description="Deliver the final read-only answer. Never proposes plan changes.",
    parameters={
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "follow_up": {"type": "boolean"},
        },
        "required": ["answer"],
        "additionalProperties": False,
    },
)
```

### 4.2 `stream_explain_turn`

```python
def stream_explain_turn(body, write_event, *, provider=None):
    """Run the read-only Explain agent and stream its progress, ending in an
    `answer` artifact. Never raises (failures -> one `error` event)."""
```

- Emit `message_started`, then `intent_selected` (`intent:"explain"`,
  `text:"Looking into your question."`).
- Build the executor for the read tools + `respond`, and run
  `loop.run(latest_user_text, provider, [read tools + RESPOND_TOOL], execute,
  history=[Message("system", EXPLAIN_PROMPT)], terminal_tools={"respond"},
  on_event=adapter)`.
- `EXPLAIN_PROMPT`: a read-only assistant that answers concisely about the
  current plan/schedule/conflicts/prior decisions, MUST ground answers via the
  read tools, MUST NOT propose or imply plan edits, and if the user is actually
  asking for a change should say it can't change the plan here (and may set
  `follow_up: true`). It finishes by calling `respond`.
- Progress adapter (Method-B, English), only for `tool_call`:
  | tool | progress text |
  |---|---|
  | `read_author_transcript` | "Reviewing the authoring history." |
  | `read_resolver_report` | "Reviewing the last resolve result." |
  | `read_plan` | "Looking at the current plan." |
  | `read_conflicts` | "Checking the current conflicts." |
  `assistant_text`, `tool_result`, `max_iters` → no default progress (CoT/detail).
- On the `respond` call, capture `answer`/`follow_up`, emit `result` with the
  answer artifact, then `message_completed` (`text` = the answer).
- Answer artifact:
  ```json
  { "type": "result",
    "artifact": { "kind": "answer", "turn_id": "...", "content": "...", "follow_up": false } }
  ```
- If the loop ends WITHOUT a `respond` call (model answered in prose or hit
  max_iters), fall back to the last assistant `content` as the answer; if none,
  emit an `error`.
- Read-tool outputs must never leak into a future Author context — that isolation
  is already handled by tagging the message `source:"explain"` on the frontend
  (which the author filter excludes). Do not tag transcript entries here.

## 5. Part C — Unified routing in `_handle_conversation_stream` (backend)

Replace the current two-way branch with classify-then-dispatch.

```
parse body; open stream; build stamped_write_event
intent = "resolve" if body.intent_hint == "resolve"
         else classify_intent(latest_user_text, state_hint, OpenAIProvider())["intent"]
emit nothing here — each stream_* fn emits its own message_started/intent_selected
if intent == "resolve":
    if resolve gate fails (status!="ready" or dirty or not in_sync or delegable==0, from body.plan_state):
        emit message_started, intent_selected(resolve),
             result(answer: precondition text), message_completed
    else:
        stream_resolve_turn(body, ...)
elif intent == "explain":
    stream_explain_turn(body, provider=OpenAIProvider())
else:  # author (default)
    stream_author_turn(body, ...)
```

- The resolve gate now lives backend-side using `body.plan_state`
  (`status`,`dirty`,`in_sync`,`delegable_conflict_count`). The backend still does
  NOT need to recompute conflicts — it trusts the frontend-provided plan_state for
  eligibility, exactly as the client-side gate did; `stream_resolve_turn`'s own
  plan-hash validation via `compile_id` remains the authoritative safety check.
- When `intent_hint == "resolve"` (button), skip the classifier (honor the hint)
  but STILL run the gate (defensive; the button is disabled when ineligible).
- The precondition answer reuses the same English text the client gate used.
- Keep `stream_author_turn` / `stream_resolve_turn` signatures unchanged; only the
  dispatcher changes.

### 5.1 Body adaptation per workflow (IMPORTANT — do not change the Phase 2/3 fns)

The unified body nests resolve data under `plan_state`, but the existing
`stream_*` functions read their own top-level fields. The dispatcher adapts:

- **author** → `stream_author_turn(body, ...)` directly. It reads top-level
  `turn_id`, `messages`, `current_actions` — all present in the unified body.
- **resolve** → call with a shallow-merged body so the Phase 3 function still sees
  top-level `plan`/`compile_id`:
  `stream_resolve_turn({**body, "plan": plan_state["completed_plan"], "compile_id": plan_state.get("compile_id")}, ...)`.
  Do NOT modify `stream_resolve_turn` to read `plan_state`.
- **explain** → `stream_explain_turn(body, ...)`; its read-tool executor reads
  `body["plan_state"][...]` and `body["messages"]` directly (per §4.1).

`latest_user_text` for the classifier and Explain = the last `role == "user"`
message content in `body["messages"]` (mirror `authoring.author`'s latest-user
selection). If there is none, the classifier is skipped and the turn errors the
same way authoring does today.

## 6. Part D — Frontend unified stream (conversationStreamClient.ts + ScenePage)

### 6.1 Unified request body + simplified `streamConversationTurn`

- `streamConversationTurn(args & {turnId}, onEvent)` now ALWAYS opens
  `POST /conversation/stream` with ONE unified body — no client-side author/resolve
  branching, and **remove the client-side resolve gate** (the backend gates now):

  ```json
  {
    "turn_id": "...",
    "intent_hint": "resolve" | null,
    "messages": [ { "role": "...", "content": "...", "source": "..." } ],
    "current_actions": [ ... ],
    "plan_state": {
      "status": "...", "dirty": false, "in_sync": true, "delegable_conflict_count": 0,
      "completed_plan": { ... }, "compile_id": "...",
      "draft_plan": { ... }, "conflicts": [ ... ], "warnings": [ ... ],
      "last_resolver_report": { ... } | null
    }
  }
  ```
  `messages` uses the SAME author context filter (exclude `source` "resolver"/
  "explain"). `current_actions` via `applyRobotOverrides(semanticActions, draftPlan)`.
- Consume the stream via the existing `readNdjsonStream`; return a `TurnOutcome`
  by terminal-artifact `kind`:
  - `author_result` → `{kind:"author_result", actions, plan, message, reason}`
  - `resolve_result` → `{kind:"resolve_result", plan, compile, message}`
  - `answer` → `{kind:"answer", intent:"explain", content}`
- Keep `runConversationTurn` (non-streaming) as the compat fallback; its unit
  tests stay valid.
- `RunTurnArgs` gains the fields needed to build `plan_state`
  (`conflicts`, `warnings`, `lastResolverReport`) — thread them from `sendConversation`.

### 6.2 `ScenePage.tsx`

- Retain the last resolver report: add `const [lastResolverReport, setLastResolverReport] = useState<unknown|null>(null)`.
  In `applyOutcome`'s `resolve_result` branch, also `setLastResolverReport(outcome.report ?? null)`
  — so extend the `resolve_result` `TurnOutcome`/artifact to carry `report` through
  to the frontend (add `report` to the resolve `TurnOutcome` and to the stream
  client's resolve mapping). Clearing the plan (`clearPlan`) resets it to null.
- In `sendConversation`, pass the extra context into `streamConversationTurn`:
  `conflicts`, `warnings` (from `usePlanCompile`), and `lastResolverReport`.
- `onEvent`, the turn_id guard, and `applyOutcome`'s three branches otherwise
  unchanged. `answer` already renders via the existing branch (source "explain").
- The Resolve button still passes `intent_hint:"resolve"` and keeps its disabled
  gate (Option A). Typing free-text now reaches the classifier (author/resolve/
  explain) via the backend.

### 6.3 Part E — Collapsible process trail (default collapsed)

Render the accumulated `activities[]` under each assistant message.

- While `status === "working"`: keep the current behavior (headline = latest
  progress text + `.think-dot`). Optionally also show the live trail; minimum is
  unchanged working headline.
- When `status === "done"` (or `error`) AND `activities.length > 0`: below the
  final content, render a collapsed toggle **"查看过程 ▸" / "Show steps ▸"**
  (choose one; match the app's existing language — the app UI is English, so use
  "Show steps"). Clicking expands a compact list of the activity `text` lines
  (dim, one per line, in `seq` order); clicking again collapses. Default state:
  collapsed.
- Implement with local component state (e.g. a `Set<messageId>` of expanded ids in
  ScenePage, or a small child component `AssistantMessage` holding its own
  `useState(false)`). A child component is cleaner — extract the assistant branch
  of the render into `AssistantMessage({ message })`.
- CSS: a `.chat-steps-toggle` (tiny, dim, button-like) and `.chat-steps` (dim
  monospace-ish list, small). Respect the existing color tokens in `style.css`.
- Applies to author, resolve, AND explain assistant messages uniformly (they all
  populate `activities[]` through the same `onEvent`).

## 7. Guardrails (must not change)

1. Resolver V2 scheduler/budgets/`compile_count`/verification, Author tools,
   compile semantics — untouched. Phase 3's `on_event` equivalence still holds.
2. `POST /author` and `POST /resolve_conflicts` endpoints unchanged.
3. Explain is READ-ONLY: its tool set has zero mutation tools; it never stages,
   compiles, or edits a plan. Its output is an `answer` artifact only.
4. Explain answers are tagged `source:"explain"` on the frontend and excluded from
   Author context (existing filter). Resolver reports likewise excluded.
5. Author still stages `{compile:false}`; resolve still adopts `compile` without
   `/compile_plan`.
6. Classifier is skipped when `intent_hint == "resolve"` (button honored).
7. Progress/answer text never leaks step ids, fingerprints, raw tool JSON, or
   session keys.

## 8. Tests

### Backend (`tests/`, pytest via `uv run --with pytest pytest ... -q`)

New file(s); do not modify existing tests.

1. `classify_intent`: with a fake provider whose `force_tool` returns each intent,
   assert the returned intent; invalid intent → `author` fallback.
2. Routing: a fake classifier + fake stream fns; assert `intent_hint:"resolve"`
   skips the classifier; a classified `resolve` with a failing gate emits an
   `answer` (precondition) and no `resolve_result`; a classified `explain` calls
   `stream_explain_turn`; default/author calls `stream_author_turn`.
3. `stream_explain_turn`: with a scripted provider that calls `read_conflicts`
   then `respond({answer, follow_up})`, assert event order `message_started`,
   `intent_selected(explain)`, `progress(read_conflicts)`, `result(kind:"answer")`
   with the answer content + `follow_up`, `message_completed`. Assert the read
   tools return the body-provided data and that NO mutation occurs (there is no
   mutating tool to call — assert the tool set contains none).
4. Explain failure (provider raises / no respond and no prose) → `error`, no
   `result`.
5. Existing `tests/test_resolver_v2.py`, `tests/test_conversation_stream.py`,
   `tests/test_resolver_v2_streaming.py`, `tests/test_authoring.py` still pass
   unmodified (run them).

### Frontend (vitest, `frontend/src/conversation/`)

1. `streamConversationTurn` builds the unified body (asserts `plan_state` +
   `messages` filter + `current_actions`) and dispatches by artifact kind for all
   three: `author_result`, `resolve_result`, `answer`.
2. It no longer gates resolve client-side (a resolve with a failing gate still
   opens the stream now — the backend returns the `answer`). Update/replace the
   Phase 3 gate-fail test accordingly and CALL OUT the replacement in the report.
3. An `answer` terminal resolves `{kind:"answer"}` and (via `applyOutcome`, if a
   component test exists) does not mutate the plan.
4. A small unit/DOM test for the collapsed-by-default trail: given a done message
   with `activities`, the steps are hidden until the toggle is activated.

## 9. Definition of done

- Free-text routes via the backend classifier to author/resolve/explain; the
  Resolve button still bypasses the classifier.
- Explain answers questions read-only, streams progress, mutates nothing, and its
  answer is excluded from Author context.
- A classified resolve on an ineligible plan returns a precondition answer without
  running the resolver.
- The process trail renders collapsed by default for all assistant messages.
- Backend + frontend test suites pass; `tsc -b` and `npm run build` clean; all
  prior-phase tests pass unmodified.

## 10. Report back

Include: files created/modified; the classifier prompt + fallback behavior; the
Explain tool set (proving no mutation tool is present) and how `respond`/`follow_up`
are handled; confirmation the resolve gate moved backend and the button still
skips the classifier; which frontend test(s) were replaced and why; how the
process trail's collapsed-by-default state is implemented; and the exact backend +
frontend test commands with pass/fail counts and tsc/build results.
