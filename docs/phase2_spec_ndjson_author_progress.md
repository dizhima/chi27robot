# Phase 2 Spec — NDJSON Transport + Author Progress

> Status: implementation spec, ready to execute. First backend change of the
> unified-conversation work.
>
> Parent contracts: `phase0_unified_conversation_contracts.md`.
> Parent design: `unified_conversation_author_resolver_design.md` (§14 Phase 2).
> Prerequisite: `phase1_spec_unified_frontend_turn.md` is implemented (the
> frontend already has `sendConversation`, `ConversationMessage`, `TurnOutcome`,
> and `runConversationTurn`).
>
> Executor note: this spec is self-contained. Do not infer requirements from any
> other conversation. Everything you need is here, plus the docs above and the
> files named in section 2.

## 1. Goal and scope

Add a streaming endpoint `POST /conversation/stream` (NDJSON) and light up
**live Author progress** on the single assistant message. The Author workflow is
the only intent wired to streaming in this phase. Resolver keeps its Phase 1
(non-streaming) path until Phase 3. Explain/clarify and the LLM classifier are
Phase 4.

### In scope

- Backend: a new `POST /conversation/stream` NDJSON endpoint that runs the
  Author workflow and emits the common event envelope, driven by `authoring.author`'s
  existing `on_event` callback through a Method-B adapter.
- Backend: wire `on_event` through the Author path (today `do_author` does NOT
  pass it — this is the gap Phase 2 closes).
- Frontend: an NDJSON reader + a `streamConversationTurn` entry that forwards
  progress events to a callback and resolves with the terminal `TurnOutcome`;
  Author routes through it, Resolver still uses `runConversationTurn`.
- Frontend: update the one live assistant message as progress events arrive.

### Explicitly OUT of scope (do NOT build in Phase 2)

- No Resolver streaming / no Resolver `on_event`. (Phase 3.)
- No Explain/clarify workflow. (Phase 4.)
- No LLM intent classifier. Routing stays deterministic exactly as Phase 1:
  `intentHint === "resolve"` → Resolver (old path), else Author (new stream).
- No changes to Resolver V2 (`resolver_v2.py`), `resolve_conflicts.py`, or the
  `/resolve_conflicts` endpoint.
- No `plan_state.revision`. Guard is still `turn_id`.
- Do NOT remove or change the existing `POST /author` and `POST /resolve_conflicts`
  endpoints — they remain for rollback/compat and are still used (resolve).
- Do NOT change compile behavior, snapshot handling, or any Resolver semantics.

## 2. Current code you are working with (verified facts)

### Backend — `src/mujoco_skills/orchestrator/`

`service.py`:
- `do_author(body, *, provider=None, manifest=None)` (line 87): parses `messages`
  + `current_actions`, `current_plan = authoring.parse_actions(current, manifest)`,
  `result = authoring.author(messages, current_plan, provider or OpenAIProvider(), manifest)`,
  then `plan = decompose.decompose(result.actions, manifest) if result.actions else None`,
  returns `{actions, plan, message, reason}`. **It does NOT pass `on_event`.**
- `Handler(BaseHTTPRequestHandler)` with `_send(code, payload)` (line 284) which
  sets `Content-Length` and writes the whole body once (NOT usable for streaming),
  `_body()` (line 294) reads/parses the request JSON, `do_OPTIONS` (204 + CORS),
  `do_POST` (line 310) with a route table `{ "/ground", "/author", "/resolve_conflicts" }`.
- CORS headers used by `_send`: `Access-Control-Allow-Origin: *`,
  `Access-Control-Allow-Headers: Content-Type`, `Access-Control-Allow-Methods: GET,POST,OPTIONS`.
- `ThreadingHTTPServer` (each request on its own thread — a synchronous
  per-line write in the handler thread is fine).

`authoring.py`:
- `author(messages, current_plan, provider, manifest, *, max_iters=..., on_event=None)`
  (line 58). `on_event` is already threaded into `loop.run(...)` and into the
  force-tool recovery path. It fires for the four loop events below.

`loop.py`:
- `run(..., on_event=None)` emits exactly four event kinds via `on_event(kind, payload)`:
  - `"assistant_text"` — payload is the model's intermediate prose string.
  - `"tool_call"` — payload is a `ToolCall` with `.name` and `.arguments`.
  - `"tool_result"` — payload is a `(name, result)` tuple.
  - `"max_iters"` — payload is the iteration cap int.
- Author tool names that appear as `tool_call.name`: `augment`, `reassign`, `propose_plan`.

### Frontend — `frontend/src/`

- `config.ts`: `orchestratorUrl` (default `http://127.0.0.1:8900`).
- `conversation/conversationTypes.ts`: `ConversationMessage`, `Intent`, `IntentHint`,
  `TurnOutcome` (Phase 1). `TurnOutcome` author variant is
  `{ kind: "author_result"; actions; plan; message; reason }`.
- `conversation/conversationRunner.ts`: `runConversationTurn(args): Promise<TurnOutcome>`
  and `RunTurnArgs` / `ResolveGate`. Author branch filters `source !== "resolver" && source !== "explain"`
  and calls `requestAuthor`.
- `ScenePage.tsx`: `sendConversation(text, intentHint?)` builds the user + working
  placeholder messages, calls `runConversationTurn`, guards by `activeTurnRef`,
  and applies the terminal outcome via `applyOutcome`. `finalizeMessage(id, patch)`
  updates one message in place.

## 3. Event contract (recap from phase0 §4, this is what you emit)

One JSON object per NDJSON line. Common fields: `type`, `turn_id`, `seq`
(monotonic from 1 per turn), optional `intent`, `stage`, `text`, `details`.

Emitted event types for the Author turn, in order:

| # | type | fields | when |
|---|---|---|---|
| 1 | `message_started` | `turn_id`, `seq` | first line |
| 2 | `intent_selected` | `intent:"author"`, `text:"Understanding your plan change."` | before the loop runs |
| n | `progress` | `stage`, `text` | per Method-B mapping (section 5) |
| n | `warning` | `text`, `details` | e.g. loop hit `max_iters` |
| k | `result` | `artifact` (see below) | after decompose succeeds |
| k+1 | `message_completed` | `text` (final summary = author message) | last line on success |
| — | `error` | `text`, optional `details` | on any exception; NO result emitted |

Author result artifact (phase0 §5.1, plus `message` for the frontend outcome):

```json
{
  "type": "result",
  "turn_id": "turn-123",
  "seq": 7,
  "artifact": {
    "kind": "author_result",
    "turn_id": "turn-123",
    "actions": [],
    "plan": { "tasks": [] },
    "message": "…",
    "reason": null
  }
}
```

`error` event example:

```json
{ "type": "error", "turn_id": "turn-123", "seq": 3, "text": "authoring failed: …" }
```

## 4. Backend implementation

Keep HTTP plumbing separate from event logic so the logic is unit-testable
without a socket.

### 4.1 New module `src/mujoco_skills/orchestrator/conversation.py`

Contains the envelope builders, the Method-B author mapping, and a testable core
that emits events through an injected `write_event` callable.

```python
AUTHOR_PROGRESS = {
    "augment": "Filling in shared-facility open/close/dependency steps.",
    "reassign": "Adjusting robot assignments.",
    "propose_plan": "Plan structure fixed; generating execution steps.",
}

def stream_author_turn(body, write_event, *, provider=None, manifest=None):
    """Run the Author workflow, emitting envelope events via write_event(dict).

    write_event receives a fully-formed event dict WITHOUT seq/turn_id; the
    caller (or this fn) stamps seq monotonically and turn_id from body. Returns
    None. Raises on unrecoverable authoring errors AFTER emitting an error event
    is the caller's job — prefer emitting `error` here and returning.
    """
```

Requirements for `stream_author_turn`:
- Read `turn_id`, `messages`, `current_actions` from `body` (same validation as
  `do_author`). Missing/invalid → emit one `error` event and return.
- Emit `message_started`, then `intent_selected` (`intent:"author"`, the
  "Understanding your plan change." text).
- Build an `on_event` adapter and pass it to `authoring.author(...)`:
  - `"tool_call"`: if `payload.name in AUTHOR_PROGRESS`, emit a `progress` event
    with `stage=payload.name`, `text=AUTHOR_PROGRESS[name]`. Otherwise ignore.
  - `"assistant_text"`: do NOT emit a default `progress` event (this is model
    chain-of-thought). At most, include it in a `details`-only debug event —
    for Phase 2, skip it entirely.
  - `"tool_result"`: skip (details-only; skip in Phase 2).
  - `"max_iters"`: emit a `warning` event (`text` = a short English note).
- After `authoring.author` returns, run `decompose.decompose(result.actions, manifest)`
  (mirroring `do_author`), then emit a `progress` event
  `stage="decomposition", text="Draft ready for your review and compile."`.
- Emit the `result` event with the author artifact
  (`actions` = `[asdict(a) for a in result.actions]`, `plan`, `message`, `reason`).
- Emit `message_completed` with `text = result.message`.
- Wrap the whole body in try/except: on any exception emit a single `error`
  event with `text=str(exc)` and return without a `result`.

seq stamping: keep a counter in the caller closure so every emitted line gets a
strictly increasing `seq` (start at 1) and the `turn_id` from `body`.

### 4.2 `service.py` — add the streaming route

- Add a streaming responder to `Handler`, e.g. `_open_ndjson_stream()` that:
  - Writes `200` status, headers `Content-Type: application/x-ndjson; charset=utf-8`,
    the same three CORS headers as `_send`, and does **NOT** set `Content-Length`.
  - Ensures the connection closes at end of stream so the client reads to EOF:
    set `self.close_connection = True`. (Note: `BaseHTTPRequestHandler`'s default
    `protocol_version` is HTTP/1.0, so omitting `Content-Length` + closing the
    connection is a valid stream the browser `fetch` reader consumes
    incrementally. Do not switch to HTTP/1.1 chunked unless you verify it end to
    end.)
  - Returns a `write_event(obj: dict)` closure that JSON-encodes `obj` +
    `"\n"`, writes to `self.wfile`, and **flushes** (`self.wfile.flush()`) after
    every line so events are not buffered until the end.
- In `do_POST`, add route `"/conversation/stream"`:
  - Parse body via `_body()`.
  - Open the stream, build a seq-stamping `write_event` wrapper (adds `seq` and
    `turn_id`), call `conversation.stream_author_turn(body, write_event, provider=OpenAIProvider(), manifest=load_manifest())`.
  - If `intent_hint == "resolve"` in the body, do NOT run Author. Emit one
    `error` event telling the client to use the legacy resolve path, then return.
    (Defensive: the frontend will not route resolve here in Phase 2.)
  - Route table for non-streaming endpoints stays exactly as is; only add the new
    streaming branch (it needs the streaming responder, not `_send`).
- Do not change `_send`, `do_author`, `do_resolve_conflicts`, `do_ground`.

### 4.3 Backend tests (`tests/`)

Match the existing pytest style (see `tests/test_resolver_v2.py`,
`tests/test_authoring.py`). Use a scripted/fake provider so no network is hit —
follow how `test_authoring.py` fakes the provider (reuse its helpers/fixtures if
present).

1. `stream_author_turn` with a fake provider that drives `augment` →
   `propose_plan` emits, in order: `message_started`, `intent_selected`,
   `progress(stage="augment")`, `progress(stage="propose_plan")`,
   `progress(stage="decomposition")`, `result`, `message_completed`. Capture
   events into a list via a fake `write_event`.
2. `seq` is strictly increasing and starts at 1; every event carries the body's
   `turn_id`.
3. The `result` artifact has `kind:"author_result"`, and its `actions`/`plan`/
   `message`/`reason` match what the non-streaming `do_author` would return for
   the same input (assert parity against a `do_author` call with the same fake
   provider + body).
4. `assistant_text` and `tool_result` loop events produce NO `progress` line.
5. An authoring failure (fake provider raises / returns no valid plan) emits an
   `error` event and NO `result`/`message_completed`.

## 5. Method-B progress mapping (authoritative, English)

| source | event emitted |
|---|---|
| turn start (before loop) | `intent_selected` text: "Understanding your plan change." |
| `tool_call` name `augment` | `progress` stage `augment`: "Filling in shared-facility open/close/dependency steps." |
| `tool_call` name `reassign` | `progress` stage `reassign`: "Adjusting robot assignments." |
| `tool_call` name `propose_plan` | `progress` stage `propose_plan`: "Plan structure fixed; generating execution steps." |
| after decompose | `progress` stage `decomposition`: "Draft ready for your review and compile." |
| `assistant_text` / `tool_result` | (nothing by default — CoT/detail only) |
| `max_iters` | `warning`: short English note |

## 6. Frontend implementation

### 6.1 New file `frontend/src/conversation/conversationStreamClient.ts`

An NDJSON reader over `fetch`, plus a `streamConversationTurn` entry.

```ts
import type { RunTurnArgs } from "./conversationRunner";
import { runConversationTurn } from "./conversationRunner";
import type { TurnOutcome } from "./conversationTypes";
import { orchestratorUrl } from "../config";

export type StreamEvent = {
  type: "message_started" | "intent_selected" | "progress" | "warning"
      | "result" | "message_completed" | "error";
  turn_id?: string;
  seq?: number;
  intent?: string;
  stage?: string;
  text?: string;
  details?: unknown;
  artifact?: {
    kind: "author_result";
    actions: unknown[];
    plan: unknown;
    message: string;
    reason: string | null;
  };
};

/** Author routes through /conversation/stream; resolve/other keep the Phase 1
 *  non-streaming path. onEvent fires for every stream event (used to update the
 *  live message). Resolves with the terminal TurnOutcome. */
export async function streamConversationTurn(
  args: RunTurnArgs & { turnId: string },
  onEvent: (event: StreamEvent) => void,
): Promise<TurnOutcome> {
  if (args.intentHint === "resolve") {
    return runConversationTurn(args);            // unchanged Phase 1 path
  }
  // author: open the NDJSON stream, forward events, resolve on `result`.
  // Parse the body as newline-delimited JSON; handle chunk splits across lines.
  // On `error` event: throw new Error(text). On `result`: capture artifact.
  // On stream end without a result: throw.
}
```

Requirements:
- Use `fetch(orchestratorUrl + "/conversation/stream", { method: "POST",
  headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
  body: JSON.stringify({ turn_id, messages, current_actions, intent_hint }),
  signal })`. Build `messages` from `args.messages` with the SAME author context
  filter as `runConversationTurn` (exclude `source "resolver"` and `"explain"`,
  map to `{role, content, source}`), and `current_actions` from
  `applyRobotOverrides(args.semanticActions, args.draftPlan)`.
- Read `response.body!.getReader()`, decode with `TextDecoder`, buffer partial
  lines, `JSON.parse` each complete line, and call `onEvent(event)`.
- Track the `result` artifact; when the stream ends, return
  `{ kind: "author_result", actions, plan, message, reason }` from that artifact.
- On an `error` event, throw `new Error(event.text ?? "author stream failed")`.
- If the response is not ok or ends with no `result`, throw a descriptive error.

### 6.2 `ScenePage.tsx` — use the stream for live updates

- In `sendConversation`, replace the `runConversationTurn(...)` call with
  `streamConversationTurn({ ...args, turnId }, onEvent)`.
- Pass an `onEvent` that, guarded by `activeTurnRef.current === turnId`, updates
  the working assistant message in place: set its `content` to the latest
  `progress`/`intent_selected`/`warning` `text` (so the newest step shows as the
  headline). Ignore `message_started`; `result`/`message_completed` are handled
  by the resolved `applyOutcome` as today.
- The `.then(applyOutcome)`, `.catch`, `.finally`, and `turn_id` guard stay
  exactly as Phase 1. Resolver still resolves with a `resolve_result` outcome via
  the unchanged branch (no live events in Phase 2, which is expected).

### 6.3 Message shape — add `activities[]` (required)

Add `activities?: { seq: number; stage?: string; text: string }[]` to
`ConversationMessage`. In the `onEvent` handler, append each
`progress`/`intent_selected`/`warning` event as one activity entry (keyed/ordered
by `seq`) to the working message, in addition to updating the headline `content`
to the latest `text`. This backs Phase 4's expandable process view. Do NOT build
the expandable UI now — just accumulate the array. The headline still shows the
latest `text` while `status === "working"`.

### 6.4 Frontend tests

Match the Phase 1 test conventions (vitest, files under `frontend/src/conversation/`).

1. An NDJSON parser test: feed a `ReadableStream` whose chunks split lines
   mid-JSON (e.g. one chunk ends in the middle of a line) and assert every event
   is parsed exactly once, in order.
2. `streamConversationTurn` author path: mock `fetch` to return a streamed body
   of `message_started` → `intent_selected` → two `progress` → `result` →
   `message_completed`; assert `onEvent` is called for each and the promise
   resolves with `{ kind: "author_result" }` carrying the artifact's fields.
3. `streamConversationTurn` with an `error` event rejects with the event text and
   never resolves an outcome.
4. `streamConversationTurn` with `intentHint: "resolve"` delegates to
   `runConversationTurn` (mock it) and does NOT open the stream.

## 7. What must NOT change (guardrails)

1. `POST /author` and `POST /resolve_conflicts` still exist and behave identically.
2. `do_author`, `do_resolve_conflicts`, `do_ground`, `_send` unchanged.
3. No changes to `resolver_v2.py`, `resolve_conflicts.py`, `authoring.author`'s
   signature (you only START passing its existing `on_event`), or `loop.py`.
4. Resolver path in the frontend still uses `runConversationTurn` and still
   adopts `result.compile` without `/compile_plan`.
5. Author still stages a draft with `{compile:false}` (unchanged in `applyOutcome`).
6. Author context filter (`source` excludes `resolver`/`explain`) is preserved in
   the streaming request builder.
7. `assistant_text` (model reasoning) is never shown as default progress.

## 8. Acceptance / definition of done

- `POST /conversation/stream` emits ordered NDJSON events for an Author turn and
  flushes per line (not buffered to the end).
- The frontend live message updates through the progress events and finalizes to
  the author draft outcome, staged with `{compile:false}`.
- Resolver behavior is byte-for-byte unchanged (still Phase 1 path).
- Backend pytest and frontend vitest suites pass; `npm run build` / `tsc -b` and
  the Python test run are green.
- Old endpoints untouched; no Resolver files touched.

## 9. Report back

Include: files created/modified; how you handled NDJSON streaming in the stdlib
handler (headers, connection close, flush); confirmation that `assistant_text`
never becomes progress; the parity assertion result between `stream_author_turn`
and `do_author`; and the exact backend + frontend test commands with pass/fail
summaries.
