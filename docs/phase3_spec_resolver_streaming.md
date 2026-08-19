# Phase 3 Spec — Resolver Event Sink + Streaming

> Status: implementation spec, ready to execute. The most safety-critical phase:
> it adds PURE OBSERVATION events to Resolver V2 and streams a resolve turn. It
> must not change any scheduling, budget, verification, or compile semantics.
>
> Parent contracts: `phase0_unified_conversation_contracts.md` (§8.2 resolver map).
> Parent design: `unified_conversation_author_resolver_design.md` (§14 Phase 3).
> Resolver reference: `conflict_resolution_scheduler_design.md`.
> Prerequisites: Phase 1 + Phase 2 are implemented (streaming author works;
> `POST /conversation/stream` exists; frontend `streamConversationTurn` exists).
>
> Executor note: self-contained. Do not infer requirements elsewhere.

## 1. Goal and scope

Give the Resolver the same live progress the Author got in Phase 2:

- Add an OPTIONAL `on_event` sink to `run_resolution_v2` and emit typed
  observation events at existing points in the loop.
- Extend `POST /conversation/stream` to handle `intent_hint == "resolve"` by
  running the resolver with an event adapter and returning a `resolve_result`
  terminal artifact (`plan` + `compile` + `report`).
- Frontend: route the resolve turn through the stream (live progress) instead of
  the Phase 1 non-streaming delegate, keeping the deterministic eligibility gate
  and the "adopt compile without /compile_plan" behavior.

### THE OVERRIDING CONSTRAINT

Resolver events are **pure observation**. Attaching `on_event` MUST NOT change a
single scheduling decision, budget count, compile, verification result, returned
plan, or `report` field. This is enforced by an equivalence test (section 4.3):
the same scripted run with and without `on_event` must produce byte-identical
`report` and returned plan. If you cannot add an event at a point without
altering control flow or state, do not add it there.

### In scope

- `run_resolution_v2(on_event=None)` + event emissions.
- Method-B resolver→English progress mapping (phase0 §8.2).
- A reusable resolve core so `/resolve_conflicts` (non-streaming) and the stream
  share one implementation; the stream passes an `on_event` adapter.
- `/conversation/stream` resolve branch + `resolve_result` artifact.
- Frontend resolve streaming.

### Explicitly OUT of scope

- No change to the scheduler, queue ordering, session/global budgets, one-tool-
  per-round rule, `_verify_candidate`, causal-floor logic, snapshot handling,
  `compile_count` accounting, or `_public_compile_result` stripping.
- No Explain/clarify, no LLM classifier (Phase 4).
- No `plan_state.revision` (turn_id guard only).
- V1 resolver stays non-streaming; the stream may emit only coarse
  start/complete events for V1 and must NOT try to synthesize fine-grained events
  by changing V1 behavior. (Simplest: the stream path runs the configured
  resolver; if `RESOLVER_VERSION=v1`, only `run_started`/`run_completed` fire.)

## 2. Current code (verified facts)

### `src/mujoco_skills/orchestrator/resolver_v2.py`

`run_resolution_v2(plan, compile_fn, provider, *, initial_compile_result=None,
initial_snapshot_reused=False, global_attempt_cap=..., session_cap=...,
return_compile_result=False)` (line 631). Structure:

- Builds `state`, `state.rebuild_queue()` (line 655).
- `while state.queue and state.attempts_used < global_attempt_cap and len(state.rounds) < llm_round_cap:` (line 658)
  - `focus = state.queue[0]` (line 660); `ledger = state.sessions[focus.session_id]`.
  - `payload = build_focus_payload(state, focus)` (662).
  - `reply = provider.chat(...)`; normalize `calls` (669-671).
  - Validate calls; pick `selected` (674-685).
  - If `selected is None`: append a no-tool round record, `state.rebuild_queue()`, `continue` (686-726).
  - Else: `tool, args = selected[...]`; ledger/action-key bookkeeping (730-734).
  - `causal_floor = _causal_floor(...)`; `candidate = deepcopy(state.plan)` (737-742).
  - `try: ... _apply(...); post = compile_fn(candidate); state.compile_count += 1; ledger.attempts_used += 1; state.attempts_used += 1; accepted, reason, post_plan, new_conflicts = _verify_candidate(...)` (747-760).
  - `if accepted: ...commit state.plan/compile_result, ledger updates...` else `ledger.failed_action_keys.add(...)` (765-782).
  - Build `record` via `_round_record(...)`, append to `state.rounds`, append tool results to `state.messages`, `state.rebuild_queue()` (784-808).
- After loop: mark residual reasons; `report = _build_report(state, global_attempt_cap)` (810-818); return `(state.plan, report[, state.compile_result])`.

`focus` fields: `focus.fingerprint`, `focus.session_id`, `focus.start`,
`focus.conflict` (a dict with `kind`, `class`, `steps`, `robots`, `window`,
`detail.facility`, `parties`).

### `src/mujoco_skills/orchestrator/service.py`

`do_resolve_conflicts(body, *, provider=None, compile_fn=None,
snapshot_lookup_fn=None, resolver_version=None)` (line 222): validates `plan`;
for v2 resolves snapshot via `compile_id` (fallback compile if invalid), calls
`run_resolution_v2(flat_plan, compile_fn, provider, initial_compile_result=...,
initial_snapshot_reused=..., return_compile_result=True)`, then returns
`{plan: _nest_resolved_plan(final_flat), report, message: render_v2_report(report),
initial_snapshot_reused, compile: _public_compile_result(final_compile_result, final_flat)}`.
Also: `_handle_conversation_stream` (Phase 2, line ~321) currently emits an
`error` for `intent_hint == "resolve"`. `conversation.stream_author_turn` lives in
`conversation.py`.

### Frontend

`conversation/conversationStreamClient.ts` `streamConversationTurn`: if
`intentHint === "resolve"` it currently delegates to `runConversationTurn(args)`
(non-streaming). `runConversationTurn`'s resolve branch checks the gate
(`status/dirty/inSync/delegableConflictCount`) and calls
`requestConflictResolution(completed, compileId)`. `ScenePage.applyOutcome`
handles `resolve_result` by `adoptCompileResult(seed, compile)` +
`stageSeededPlan(seed, {compile:true})` (no `/compile_plan`).

## 3. Resolver events (phase0 §8.2) — observation points

Add `on_event: Callable[[str, dict], None] | None = None` as the LAST keyword arg
of `run_resolution_v2`. Emit via a local helper `emit(kind, **data)` that is a
no-op when `on_event is None`. Placement (all are read-only w.r.t. state):

| event kind | where | payload data |
|---|---|---|
| `run_started` | right after `state.rebuild_queue()` (before the while) | `queued_conflicts=len(state.queue)` |
| `focus_started` | just after `focus = state.queue[0]` | `facility`, `robots`, `kind`, `class`, `window` from `focus.conflict` |
| `strategy_selected` | after `selected` is chosen (the non-None branch), before `_apply` | `tool`, `robots`/`facility` |
| `verification_started` | immediately before `post = compile_fn(candidate)` | `tool` |
| `round_completed` | after the `record` is appended to `state.rounds` | `accepted` (bool), `reason`, `tool`, `facility` |
| `session_deferred` | where a session first becomes `status == "exhausted"` OR a conflict is recorded in `state.deferred_conflicts` (end-of-run bookkeeping, lines ~810-817, and/or inside rebuild when session_cap defers) | `session_id`, `reason` |
| `run_completed` | after `report = _build_report(...)`, before return | `converged=report["converged"]`, `rounds_used=report["rounds_used"]` |

Rules:
- `emit` must be called with data already available; do NOT compute anything new
  that could throw and change flow. Wrap the whole emit in nothing — `emit` itself
  must swallow exceptions from `on_event` (a bad sink must never break resolution).
- Do NOT emit inside `_verify_candidate`, `_apply`, `build_focus_payload`, or any
  helper — only at the `run_resolution_v2` loop level, so the equivalence test can
  bracket it.
- For `session_deferred`, if a clean single hook is awkward, emit it once per
  session that ends non-active in the end-of-run pass (lines ~810-817). Do not add
  branches to the hot loop for it.

## 4. Backend implementation

### 4.1 Method-B resolver mapping — in `conversation.py`

```python
def resolver_progress_text(kind: str, data: dict) -> str | None:
    # Returns display text, or None if this event has no default progress line.
    ...
```

Mapping (English, phase0 §8.2). Interpolate facility/robots when present; never
leak step ids, fingerprints, tool JSON, or session keys into the text:

| kind | text |
|---|---|
| `run_started` | "Checking conflicts in the compiled plan." |
| `focus_started` | "Working on the overlap near {facility} between {robots}." (fall back to "…between the robots." when unknown) |
| `strategy_selected` | "Trying to adjust execution order so the occupant leaves earlier." |
| `verification_started` | "Change generated; running a full compile to verify." |
| `round_completed` accepted | "That adjustment worked; checking remaining conflicts." |
| `round_completed` rejected | "That adjustment did not help; trying another option." |
| `session_deferred` | "This conflict group hit its attempt limit; kept for the next Resolve." |
| `run_completed` | "Final verification done; producing the summary." |

### 4.2 Reusable resolve core

Refactor so there is ONE implementation of the v2 resolve pipeline (snapshot
reuse, fallback compile, `run_resolution_v2`, `compile_count += 1` on fallback,
nesting, public compile). Extract from the current `do_resolve_conflicts` body a
function, e.g.:

```python
def resolve_v2_core(body, *, provider, compile_fn, snapshot_lookup_fn, on_event=None):
    """Returns the same dict do_resolve_conflicts returns for v2, passing
    on_event through to run_resolution_v2. Behavior identical to today when
    on_event is None."""
```

- `do_resolve_conflicts` (v2 path) becomes a thin caller of `resolve_v2_core`
  with `on_event=None`. The v1 path and the public API/return shape stay
  identical. Every existing test in `tests/test_resolver_v2.py` must still pass
  UNCHANGED (do not edit those tests except to add new ones).
- The `compile_count += 1` on fallback (service.py:255-256) must remain exactly
  where/how it is relative to the core; the equivalence and existing
  `compile_count` tests (0 reused / 1 fallback) must still hold.

### 4.3 `/conversation/stream` resolve branch

In `_handle_conversation_stream`, replace the resolve `error` with a real run.
Add to `conversation.py`:

```python
def stream_resolve_turn(body, write_event, *, provider=None, compile_fn=None,
                        snapshot_lookup_fn=None):
    """Run the v2 resolve core with an event adapter, emit the envelope, and end
    with a resolve_result artifact. Never raises: failures become one `error`."""
```

Requirements:
- Emit `message_started`, then `intent_selected` (`intent:"resolve"`,
  `text:"Checking the compiled plan for conflicts."`).
- Build `on_event = adapter` that maps `(kind, data)` → `resolver_progress_text`;
  when it returns a string, `write_event({"type":"progress","stage":kind,"text":...})`.
  `session_deferred` may instead be a `warning`. Unmapped kinds emit nothing.
- Call `resolve_v2_core(body, provider=..., compile_fn=..., snapshot_lookup_fn=...,
  on_event=on_event)`.
- On success emit `result` with the resolve artifact, then `message_completed`
  (`text` = a one-line English summary; you MAY derive it from `report`, e.g.
  "Resolved N conflicts." / "Returned the last verified partial plan.").
- The artifact:
  ```json
  {
    "type": "result",
    "artifact": {
      "kind": "resolve_result",
      "turn_id": "...",
      "plan": { "tasks": [] },
      "compile": { "schedule": [], "warnings": [], "conflicts": [], "completed": {} },
      "report": {},
      "initial_snapshot_reused": true
    }
  }
  ```
  `plan` = `_nest_resolved_plan(...)`, `compile` = `_public_compile_result(...)` —
  i.e. exactly what `do_resolve_conflicts` returns, just delivered as a stream.
- On any exception: emit ONE `error` event and no `result`.
- Body for resolve stream carries `plan` (completed-plan object) and `compile_id`
  (same as `/resolve_conflicts`), plus `turn_id` and `intent_hint`.

In `service.py`, `_handle_conversation_stream`: if `intent_hint == "resolve"`,
call `conversation.stream_resolve_turn(body, stamped_write_event, provider=OpenAIProvider(),
compile_fn=_compile_via_skill_service, snapshot_lookup_fn=_snapshot_via_skill_service)`;
else keep the Phase 2 `stream_author_turn` path. Do NOT change `do_resolve_conflicts`'s
route or `/resolve_conflicts`.

### 4.4 Backend tests (`tests/`)

Reuse `tests/test_resolver_v2.py` style (ScriptedProposer, fake compile). Add a
new test file; do NOT modify existing resolver tests.

1. **Equivalence (the critical one):** run `run_resolution_v2` on a scripted
   multi-round scenario twice — once with `on_event=None`, once with an
   `on_event` that appends to a list — and assert the returned plan and the full
   `report` are equal (`==`). Assert the recording sink captured a non-empty,
   ordered event list (so the test proves events fire without changing output).
2. `run_started` fires once before any round; `run_completed` fires once after,
   with `converged` matching `report["converged"]`.
3. `focus_started` count equals the number of loop iterations; `verification_started`
   count equals the number of executed candidates (i.e. `report["compile_count"]`
   minus any initial compile) — assert the observation counts line up with the
   existing round records, NOT that they change anything.
4. `stream_resolve_turn` with a fake core/provider emits, in order:
   `message_started`, `intent_selected(intent:"resolve")`, ≥1 `progress`,
   `result(kind:"resolve_result")` whose `plan`/`compile`/`report` equal what
   `do_resolve_conflicts` returns for the same input, then `message_completed`.
5. A resolve failure emits `error` and no `result`.
6. Assert existing `tests/test_resolver_v2.py` still passes unchanged (run it).

## 5. Frontend implementation

### 5.1 `conversationStreamClient.ts`

- Generalize the stream so it also handles resolve. `streamConversationTurn`:
  - Keep the resolve **eligibility gate** client-side (same as `runConversationTurn`:
    if `status !== "ready" || dirty || !inSync || delegableConflictCount === 0`,
    return `{ kind: "answer", intent: "resolve", content: <precondition text> }`
    WITHOUT opening the stream).
  - If the gate passes, open `POST /conversation/stream` with body
    `{ turn_id, intent_hint: "resolve", plan: args.completed, compile_id: args.compileId }`.
  - Forward events to `onEvent`. Capture the `resolve_result` artifact and resolve
    with `{ kind: "resolve_result", plan: artifact.plan, compile: artifact.compile,
    message: <from message_completed or a summary> }`.
  - Author path unchanged. Reuse the same NDJSON reader/partial-line buffering.
- `StreamEvent.artifact` becomes a union of the author and resolve artifact shapes.
- Do NOT delete `runConversationTurn`; it stays as the non-streaming fallback and
  its unit tests stay valid. (It is simply no longer the resolve path inside
  `streamConversationTurn`.)

### 5.2 `ScenePage.tsx`

- No structural change needed to `sendConversation`/`onEvent`: the existing
  `onEvent` already updates the working message from `progress`/`intent_selected`/
  `warning` text, so resolver progress lines light up automatically.
- `applyOutcome`'s `resolve_result` branch is unchanged (`adoptCompileResult` +
  `stageSeededPlan({compile:true})`, no `/compile_plan`). Confirm the streamed
  `compile` payload flows into it exactly as the non-streaming one did.
- The Resolve button still passes `intent_hint:"resolve"` and keeps its disabled
  gate (Option A).

### 5.3 Frontend tests

Add to `conversation/`:
1. `streamConversationTurn` resolve happy path: mock a streamed body
   `message_started → intent_selected(resolve) → progress×2 → result(resolve_result) → message_completed`;
   assert `onEvent` fires per event and it resolves `{ kind:"resolve_result" }` with
   the artifact's `plan`/`compile`.
2. resolve gate-fail returns `{ kind:"answer" }` and does NOT open the stream
   (fetch not called).
3. Author path test from Phase 2 still passes (regression).

## 6. Guardrails (must not change)

1. Scheduler, queue order, one-tool-per-round, session/global budgets,
   `_verify_candidate`, causal floors, snapshot-as-round-0, `compile_count`
   accounting, `_public_compile_result` stripping — all byte-for-byte unchanged.
2. `on_event` is optional and defaults to None; with None, `run_resolution_v2` is
   identical to today (proven by the equivalence test).
3. `do_resolve_conflicts` public behavior and `/resolve_conflicts` endpoint
   unchanged; existing resolver tests pass unmodified.
4. Frontend resolve still adopts the returned `compile` without `/compile_plan`;
   Author path from Phase 2 unchanged.
5. Resolver progress text never leaks step ids, fingerprints, tool JSON, or
   session keys (those go in `details` only, if at all).
6. A throwing `on_event` sink must not break resolution (emit swallows).

## 7. Definition of done

- `run_resolution_v2` accepts `on_event`; the equivalence test proves output is
  unchanged with vs without it.
- A resolve turn streams live progress through `/conversation/stream` and ends in
  a `resolve_result` the frontend adopts without `/compile_plan`.
- Backend: new tests pass AND `tests/test_resolver_v2.py` passes unchanged
  (run via `uv run --with pytest pytest tests/<files> -q`).
- Frontend: `npx vitest run` and `npx tsc -b` clean.
- No scheduler/budget/compile semantics changed; V1 stays non-streaming.

## 8. Report back

Include: files created/modified; the exact event injection points (line-level)
and confirmation none of them read or write loop state in a way that changes it;
the equivalence-test result (plan + report equal with/without sink); confirmation
`tests/test_resolver_v2.py` passed unmodified; how `session_deferred` was hooked;
and the backend + frontend test commands with pass/fail counts.
