# Phase 0 Contracts — Unified Conversation

> Status: contract draft for review; not implemented.
>
> Updated: 2026-08-02.
>
> Scope: freeze the intent set, eligibility gates, streaming event envelope,
> terminal artifacts, turn guard, and the read-only Explain tool surface, so
> Phase 1+ can be built and tested against fixed shapes. This document does not
> introduce streaming transport or workflow chaining; it only fixes the
> contracts. Parent design: `unified_conversation_author_resolver_design.md`.

## 0. Locked product decisions

These were confirmed in discussion and are treated as fixed for Phase 0:

| # | decision |
|---|---|
| one-turn-one-workflow | A user turn routes to exactly one workflow. No automatic Author→Compile→Resolver chaining. |
| routing | Hybrid: deterministic UI hint + a small typed **LLM classifier** for language. No large deterministic phrase engine required. |
| explain/clarify | A single **read-only LLM agent** backs both. clarify = the same agent ending in a question instead of an answer. |
| explain-tools | Four read-only tools, `plan` and `conflicts` split. |
| progress-text | **Method B — semantic mapping**: translate typed tool/resolver events into fixed human strings. |
| progress-language | Default **English**. |
| v2-report | `render_v2_report` is **demoted to technical detail**; it is never the default chat message. Progress + final summary are generated deterministically from typed events. |
| guard | **`turn_id` only**. No `plan_state.revision` in v1 (editing is disabled during a running turn, and only one mutating workflow runs at a time, so the stale-edit window does not exist yet). |
| editing | Plan editing is disabled while a conversation turn (routing / model call / compile) is running. |

Still open (defaults proposed, not blocking Phase 0): Compile as an explicit
chat intent (default: button only), and whether completed messages collapse the
activity timeline by default (default: collapsed).

## 1. Intent set

```text
author   -> mutating   -> Author workflow
resolve  -> mutating   -> Resolver workflow (V2 default, V1 via RESOLVER_VERSION)
explain  -> read-only  -> Explain agent (answers)
clarify  -> read-only  -> Explain agent (asks one focused question)
```

`compile` is not an intent in v1; Compile remains a frontend button.

### 1.1 Classifier output

The LLM classifier is consulted only after a validated UI hint and is a single
typed call:

```json
{
  "intent": "author",
  "confidence": 0.94,
  "reason_code": "explicit_task_change"
}
```

- `reason_code` is telemetry/test-only; the UI shows a stable human label, never
  this value.
- Explicit semantic edits win over the mere presence of conflicts. Example:
  "move apple_2 to the sink" routes to `author` even when conflicts exist.

### 1.2 Routing order

1. Validated explicit UI hint (Resolve button sends `intent_hint: "resolve"`).
2. LLM classifier for everything else.
3. Deterministic eligibility gate (section 2) validates the selected intent
   against current state.
4. Route, or return a non-mutating clarify / precondition reply.

The classifier proposes; the deterministic gate disposes. A high-confidence
`resolve` cannot enter the Resolver if the gate fails.

## 2. Eligibility matrix (deterministic, classifier cannot bypass)

| intent | required state | on failure |
|---|---|---|
| `author` | manifest + semantic action state available | return authoring error (no mutation) |
| `resolve` | `status == "ready"` and `!dirty` and `in_sync` and `delegable_conflict_count > 0` | `answer` explaining the exact unmet precondition |
| `explain` | any available plan/compile projection | answer from available state |
| `clarify` | none | ask one focused question; never mutate |

The four resolve signals already exist in `ScenePage.tsx` (`status`, `dirty`,
`inSync`, `delegableConflictCount`). Phase 1 lifts them into the request as
`plan_state`; the backend still re-verifies the plan hash before Resolver runs.

## 3. Request — `POST /conversation/stream`

```http
POST /conversation/stream
Content-Type: application/json
Accept: application/x-ndjson
```

```json
{
  "turn_id": "turn-123",
  "messages": [{ "id": "m1", "role": "user", "content": "解决现在的冲突" }],
  "current_actions": [],
  "plan_state": {
    "dirty": false,
    "in_sync": true,
    "draft_plan": { "tasks": [] },
    "completed_plan": { "robot0": [], "robot1": [] },
    "compile_id": "compile-456",
    "compile_summary": { "status": "ready", "delegable_conflict_count": 3 }
  },
  "intent_hint": null
}
```

Mandatory identity fields:

- `turn_id` — identifies the live assistant message and the event stream; the
  sole staleness guard in v1.
- `compile_id` — lets Resolver validate and reuse the exact compile snapshot.
- Backend normalized plan-hash validation remains authoritative.

`plan_state.revision` is intentionally absent in v1.

## 4. Event envelope (NDJSON)

One JSON object per line. Ordered and idempotent by `(turn_id, seq)`.

```json
{
  "type": "progress",
  "turn_id": "turn-123",
  "seq": 4,
  "intent": "resolve",
  "stage": "verification",
  "text": "Reordered execution; recompiling to verify.",
  "details": {}
}
```

Common fields: `type`, `turn_id`, `seq`, optional `text` (already display-safe),
optional `details` (structured technical data, hidden by default).

| type | purpose |
|---|---|
| `message_started` | create/init the one live assistant message for this turn |
| `intent_selected` | announce the chosen workflow in human language |
| `progress` | one typed observable step (Method B text) |
| `warning` | recoverable limitation / deferred work |
| `result` | carries the terminal artifact (the only mutating event) |
| `message_completed` | finalize the assistant message |
| `error` | terminate without committing any mutation |

Model chain-of-thought (`assistant_text` from the loop) is never emitted as a
default `progress` event; it may only appear inside `details` for a debug view.

## 5. Terminal artifacts (only `result` may commit)

### 5.1 Author

```json
{
  "type": "result",
  "artifact": {
    "kind": "author_result",
    "turn_id": "turn-123",
    "actions": [],
    "plan": { "tasks": [] },
    "reason": null
  }
}
```

Frontend stages `plan` as a draft. No automatic compile.

### 5.2 Resolver

```json
{
  "type": "result",
  "artifact": {
    "kind": "resolve_result",
    "turn_id": "turn-123",
    "plan": { "tasks": [] },
    "compile": { "schedule": [], "warnings": [], "conflicts": [], "completed": {} },
    "report": {},
    "initial_snapshot_reused": true
  }
}
```

Frontend adopts `compile` directly (no second `/compile_plan`). Backend keeps
the existing normalized-hash validation between `plan` and `compile.completed`,
and the `_public_compile_result` private-geometry stripping. `report` is
technical detail only; the chat summary is generated from typed events.

### 5.3 Explain / clarify

```json
{
  "type": "result",
  "artifact": {
    "kind": "answer",
    "turn_id": "turn-123",
    "content": "One path conflict near the sink remains.",
    "follow_up": false
  }
}
```

Never mutates plan state. `follow_up: true` marks a clarify question. This
message is tagged so it is **excluded from Author context** (section 7).

## 6. Turn guard (v1)

The frontend applies a terminal artifact only when
`artifact.turn_id == active_turn_id`. Any artifact from a superseded turn is
dropped and reported as not applied. Only one mutating workflow runs at a time;
plan editing is disabled while a turn is active. No revision counter is used.

## 7. Context isolation

The visible transcript is a product conversation, not one model context.

- **Author** receives: user semantic instructions, final Author summaries
  (`propose_plan` message), and `current_actions` after robot overrides.
  Excludes: Resolver progress/report, low-level tool JSON, compile warnings
  already in `plan_state`, and Explain/clarify answers.
- **Resolver** receives: the synchronized completed plan, the validated compile
  snapshot handle, and its own bounded V2 session history. Excludes the general
  transcript. (Unchanged from today.)
- **Explain/clarify** reads only through the four tools in section 9.

Messages are tagged by producer (`author` / `resolver` / `explain`). The backend
builds each workflow's input from tags, not from the display list. This
generalizes the existing `source !== "resolver"` filter already in `sendChat`.

## 8. Method-B progress mapping (default English)

### 8.1 Author (from `on_event` `tool_call` name)

The four loop events are `assistant_text`, `tool_call`, `tool_result`,
`max_iters`. Only `tool_call` drives default progress; the rest go to `details`.

| tool_call name | progress text |
|---|---|
| request accepted | Understanding your plan change. |
| `augment` | Filling in shared-facility open/close/dependency steps. |
| `reassign` | Adjusting robot assignments. |
| `propose_plan` | Plan structure fixed; generating execution steps. |
| decomposition complete | Draft ready for your review and compile. |

Phase 2 must first wire `on_event` through `do_author` (the HTTP layer does not
pass it today) before this mapping can fire.

### 8.2 Resolver (from the new `on_event` sink)

| resolver event | progress text |
|---|---|
| `run_started` | Checking conflicts in the compiled plan. |
| `focus_started` | Working on the overlap near {facility} between {robots}. |
| `strategy_selected` | Trying to adjust execution order so the occupant leaves earlier. |
| `verification_started` | Change generated; running a full compile to verify. |
| `round_completed:accepted` | That adjustment worked; checking remaining conflicts. |
| `round_completed:rejected` | That adjustment did not help; trying another option. |
| `session_deferred` | This conflict group hit its attempt limit; kept for the next Resolve. |
| `run_completed` | Final verification done; producing the summary. |

Facility/robot names may be interpolated; step ids, fingerprints, raw tool JSON,
and session keys stay in `details`. The final chat summary is generated
deterministically from these events, not from `render_v2_report`.

## 9. Explain / clarify read-only tool surface

One agentic loop, **read-only tools only**, no mutation tools of any kind.
`plan` and `conflicts` are split.

| tool | returns |
|---|---|
| `read_author_transcript` | user semantic instructions + final Author summaries |
| `read_resolver_report` | the last Resolve run's typed report / residual reasons |
| `read_plan` | current draft and completed plan (semantic + scheduled) |
| `read_conflicts` | current compile conflicts, warnings, and sync status |

The agent chooses which sources to read. Its output is an `answer` artifact
(section 5.3), tagged `explain`, excluded from Author context. clarify uses the
same tools and ends by returning `follow_up: true` with one question; it never
produces a plan and never commits.

## 10. Phase 0 deliverables (schemas + tests, no runtime wiring)

1. Type/JSON-schema definitions for: intent + classifier output, event
   envelope, the three terminal artifacts, and the request body.
2. Router-table tests over representative Chinese/English requests, asserting the
   selected intent and `reason_code`.
3. Gate tests proving Resolver eligibility cannot be bypassed by a
   high-confidence classifier result (dirty / not in-sync / zero delegable
   conflicts each block).
4. A test proving only a `result` event mutates frontend state; `progress` /
   `warning` / `error` never do.
5. A turn-guard test: an artifact whose `turn_id` != active turn is dropped.
6. A context-isolation test: Explain/clarify `answer` messages and Resolver
   reports are excluded when building Author input.
7. A Resolver equivalence guard (for Phase 3): with an event sink attached,
   `report.rounds`, `compile_count`, `converged`, and the returned plan are
   byte-for-byte identical to the no-sink run.

## 11. What stays untouched

- Resolver V2 scheduling semantics: global-earliest focus, one tool per round,
  full fresh compile per candidate, `_verify_candidate` gates, session/global
  budgets, `compile_count` accounting, snapshot-as-round-0-only, private-geometry
  stripping.
- `RESOLVER_VERSION=v1` rollback.
- Existing `POST /author` and `POST /resolve_conflicts` endpoints.
- Author's narrow tool boundary (`augment`, `reassign`, `propose_plan`).
