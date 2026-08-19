# Unified Conversation for Author and Resolver

> Status: proposal for discussion; not implemented.
>
> Updated: 2026-08-02.
>
> Purpose: merge the user experience of `/author` and `/resolve_conflicts`
> behind one conversational entry point while preserving their separate
> planning responsibilities, tool boundaries, verification rules, and rollback
> paths.

## 1. Summary

The frontend should present one conversation in which a user can create or
refine a semantic task plan, ask to resolve compiled scheduling conflicts, or
ask a read-only question. A lightweight router selects the appropriate backend
workflow. All workflows publish a common stream of structured events which the
frontend renders into one continuously updated assistant message per user
turn.

This is a **unified conversation experience**, not one unrestricted LLM agent.
Author and Resolver remain isolated workflows:

- Author interprets semantic task changes and returns `actions + draft plan`.
- Compile remains the deterministic confirmation and geometry boundary.
- Resolver consumes a synchronized compiled plan, applies bounded low-level
  repairs, and returns its last verified `plan + compile + report`.
- Explain answers questions without mutating plan state.

The first implementation should route to exactly one workflow per user turn.
Automatic `Author -> Compile -> Resolver` chaining is deferred until the
single-workflow design is stable.

```text
User message or Resolve shortcut
              |
              v
    eligibility checks + intent router
       /              |             \
      v               v              v
   Author          Resolver        Explain
      \               |              /
       +------ common event stream --+
                         |
                         v
          one live assistant message
                         |
                         v
       atomic terminal artifact commit
```

## 2. Current system

### 2.1 Author

The frontend calls `POST /author` with:

```json
{
  "messages": [{"role": "user", "content": "..."}],
  "current_actions": []
}
```

The backend runs the semantic authoring loop and deterministic decomposition.
It returns:

```json
{
  "actions": [],
  "plan": {"tasks": []},
  "message": "...",
  "reason": null
}
```

Author is stateless. The frontend resends the relevant transcript and current
semantic actions. The resulting plan is staged as a draft and is not compiled
automatically. The authoring implementation already supports an `on_event`
callback for tool calls and results.

### 2.2 Resolver

The frontend calls `POST /resolve_conflicts` with:

```json
{
  "plan": {"robot0": [], "robot1": []},
  "compile_id": "compile-id"
}
```

Resolver V2 validates and reuses the matching compile snapshot when possible,
runs bounded repair sessions with full fresh verification compiles, and
returns:

```json
{
  "plan": {"tasks": []},
  "compile": {
    "schedule": [],
    "warnings": [],
    "conflicts": [],
    "completed": {}
  },
  "report": {},
  "message": "..."
}
```

The frontend adopts the returned V2 compile result directly. It must not
compile the resolved plan again. Resolver does not consume Author chat history.

### 2.3 Current UX limitation

Author and Resolver write into the same visible chat but are initiated through
different frontend functions and pending states. Both endpoints return only
after all work finishes, so the user sees no useful progress while a request is
running. Resolver emits a technical report only at the end.

## 3. Goals

1. Accept authoring, resolving, and explanatory requests through one chat
   composer.
2. Automatically select the appropriate workflow with deterministic safety
   gates.
3. Update one assistant message throughout the complete turn, regardless of
   the selected workflow.
4. Describe observable work in natural language without exposing hidden model
   reasoning.
5. Commit plan changes only from a successful terminal result.
6. Preserve Resolver V2 verification and compile-result reuse.
7. Preserve the existing Resolve button as an optional shortcut.
8. Keep `/author` and `/resolve_conflicts` available during migration and for
   rollback/testing.

## 4. Non-goals

- Do not merge Author and Resolver tools into one unrestricted tool set.
- Do not give Resolver permission to make semantic task changes such as
  changing destinations or reassigning arbitrary work.
- Do not give Author access to low-level conflict repair tools.
- Do not expose chain-of-thought or raw model reasoning.
- Do not execute simulation or claim physical task success; this remains a
  planning interface.
- Do not introduce incremental compilation or candidate-result caching.
- Do not change Resolver V2 session budgets or verification semantics as part
  of this UI/API merge.
- Do not automatically chain Author, Compile, and Resolver in the first phase.

## 5. Core design principles

### 5.1 One conversation does not mean one model context

The visible transcript is a product-level conversation. Each workflow receives
only the context it owns:

- Author receives user semantic instructions and selected final Author
  summaries, plus `current_actions`.
- Resolver receives the synchronized completed plan, compile snapshot handle,
  and its internally bounded session history. It receives no general chat
  transcript.
- Explain receives a compact read-only projection of the current plan,
  compilation state, warnings, and conflicts.

Progress messages, raw repair calls, conflict fingerprints, and coordination
session internals must not be fed back to Author as semantic instructions.

### 5.2 Deterministic state gates precede probabilistic routing

The router may classify language, but it must not bypass state requirements.
For example, a request cannot enter Resolver when there is no synchronized
compiled plan even if the classifier reports `resolve` with high confidence.

### 5.3 State changes are atomic at terminal events

Progress events never modify the active plan. The frontend applies a new draft
or resolved plan only after receiving a valid terminal artifact for the current
turn and plan revision.

### 5.4 Narration describes actions and outcomes

User-facing progress is derived from typed workflow events. Examples include
"正在调整机器人分工" and "正在验证新的执行顺序". It should not be a second
free-form LLM narration layer and should not reveal private reasoning.

## 6. Intent model

The initial intent set should remain small:

| intent | examples | mutation | workflow |
|---|---|---:|---|
| `author` | add/remove tasks, change object/destination, change robot | yes | Author |
| `resolve` | solve conflicts, avoid collision, improve scheduling | yes | Resolver |
| `explain` | why this order, what conflicts remain, current status | no | Explain |
| `clarify` | materially ambiguous mutating request | no | ask user |

`compile` should remain an explicit frontend action in the first phase. It may
become an intent or an internal pipeline stage later.

### 6.1 Recommended routing order

1. Honor a validated explicit UI hint, such as the Resolve button's
   `intent_hint: "resolve"`.
2. Detect high-confidence semantic editing phrases deterministically.
3. Detect high-confidence resolve and explain phrases deterministically.
4. Use a typed LLM classifier only for the remaining ambiguous cases.
5. Validate the selected intent against current state.
6. Route or return a non-mutating clarification/precondition response.

Suggested typed classifier result:

```json
{
  "intent": "author",
  "confidence": 0.94,
  "reason_code": "explicit_task_change"
}
```

`reason_code` is for telemetry and tests. The frontend should display a stable
human description instead of this internal value.

### 6.2 Eligibility matrix

| selected intent | required state | behavior when requirement fails |
|---|---|---|
| `author` | manifest and semantic action state available | report authoring error |
| `resolve` | status ready, draft not dirty, compiled state in sync, delegable conflicts > 0 | explain exact unmet precondition |
| `explain` | any available plan/compile projection | answer from available state |
| `clarify` | none | ask one focused question; do not mutate |

Explicit semantic edits take precedence over the mere presence of conflicts.
For example, "move the second apple to the sink instead" routes to Author even
when the current compile contains a facility conflict.

## 7. Unified API

Add a streaming orchestration endpoint while retaining the current endpoints:

```http
POST /conversation/stream
Content-Type: application/json
Accept: application/x-ndjson
```

Suggested request:

```json
{
  "turn_id": "turn-123",
  "messages": [
    {"id": "m1", "role": "user", "content": "解决现在的冲突"}
  ],
  "current_actions": [],
  "plan_state": {
    "revision": 12,
    "dirty": false,
    "in_sync": true,
    "draft_plan": {"tasks": []},
    "completed_plan": {"robot0": [], "robot1": []},
    "compile_id": "compile-456",
    "compile_summary": {
      "status": "ready",
      "delegable_conflict_count": 3
    }
  },
  "intent_hint": null
}
```

The exact payload may later be split into compact projections, but the
following identity fields are mandatory:

- `turn_id`: identifies the live message and stream.
- `plan_state.revision`: prevents a stale result from overwriting newer edits.
- `compile_id`: lets Resolver validate and reuse the exact compile snapshot.
- normalized plan hash validation remains authoritative on the backend.

### 7.1 Common event envelope

Every line is one JSON object:

```json
{
  "type": "progress",
  "turn_id": "turn-123",
  "seq": 4,
  "intent": "resolve",
  "stage": "verification",
  "text": "执行顺序已调整，正在重新编译验证。",
  "details": {}
}
```

Required common fields:

- `type`: event type.
- `turn_id`: request identity.
- `seq`: monotonically increasing sequence number.
- `text`: optional user-facing text already safe to display.
- `details`: optional structured technical data.

Initial event types:

| type | purpose |
|---|---|
| `message_started` | create/initialize the live assistant message |
| `intent_selected` | announce the selected workflow in user language |
| `progress` | report a typed observable step |
| `warning` | report a recoverable limitation or deferred work |
| `result` | carry the terminal artifact |
| `message_completed` | finalize the assistant message |
| `error` | terminate without committing mutations |

Events should be ordered and idempotent by `(turn_id, seq)` so the frontend can
ignore duplicates.

### 7.2 Terminal artifacts

Only a `result` event may carry state to commit.

#### Author result

```json
{
  "type": "result",
  "artifact": {
    "kind": "author_result",
    "base_revision": 12,
    "actions": [],
    "plan": {"tasks": []},
    "reason": null
  }
}
```

The frontend stages `plan` as a draft and does not compile automatically.

#### Resolver result

```json
{
  "type": "result",
  "artifact": {
    "kind": "resolve_result",
    "base_revision": 12,
    "plan": {"tasks": []},
    "compile": {
      "schedule": [],
      "warnings": [],
      "conflicts": [],
      "completed": {}
    },
    "report": {}
  }
}
```

The frontend must adopt this compile payload directly. For Resolver V2 it must
not invoke `/compile_plan` again. The backend must preserve the existing
normalized hash validation between the resolved plan and
`compile.completed`.

#### Read-only answer

```json
{
  "type": "result",
  "artifact": {
    "kind": "answer",
    "base_revision": 12,
    "content": "当前还有一组 sink 附近的路径冲突。"
  }
}
```

It never updates plan state.

## 8. Workflow-specific progress mapping

### 8.1 Author

Author already has an event callback. Add a deterministic adapter from internal
events to the common envelope:

| internal event/tool | user-facing progress |
|---|---|
| request accepted | 正在理解你对任务计划的修改。 |
| `augment` | 正在补全共享设施的打开、关闭和依赖步骤。 |
| `reassign` | 正在调整机器人分工。 |
| `propose_plan` | 计划结构已确定，正在生成执行步骤。 |
| decomposition complete | 已生成执行草稿，等待你确认并编译。 |

Tool arguments and complete model responses belong in logs, not the default
chat view.

### 8.2 Resolver

Resolver should expose a typed event sink without altering its repair logic:

| resolver event | user-facing progress |
|---|---|
| `run_started` | 正在检查当前编译计划中的冲突。 |
| `focus_started` | 正在处理两个机器人在冰箱附近的重叠占用。 |
| `strategy_selected` | 将尝试调整执行顺序，让占用方更早离开。 |
| `verification_started` | 修改已生成，正在进行完整编译验证。 |
| `round_completed: accepted` | 这次调整有效，继续检查剩余冲突。 |
| `round_completed: rejected` | 这次调整未改善目标冲突，正在尝试其他方案。 |
| `session_deferred` | 该冲突组达到本轮尝试上限，将保留到下次 Resolve。 |
| `run_completed` | 完成最终验证并生成结果摘要。 |

The default text should name facilities and robots when useful, but hide step
IDs, conflict hashes, raw tool JSON, and coordination-session keys. Those can
appear in an expandable technical-details section.

Resolver V1 remains selectable through `RESOLVER_VERSION=v1`. The unified
stream may emit only coarse start/completion events for V1; it must not emulate
fine-grained events by changing V1 behavior.

## 9. Frontend message model

Replace separate append-at-completion behavior with a stable live message per
turn. A possible shape is:

```ts
type ConversationMessage = {
  id: string;
  role: "user" | "assistant";
  status: "working" | "done" | "error";
  intent?: "author" | "resolve" | "explain" | "clarify";
  summary: string;
  activities?: Array<{
    seq: number;
    stage: string;
    text: string;
    status: "active" | "accepted" | "rejected" | "done";
  }>;
  technicalDetails?: unknown[];
};
```

Recommended behavior:

1. Add the user message.
2. Immediately add one assistant placeholder with `status: "working"`.
3. Update that same message for every stream event.
4. Show the latest activity prominently while preserving prior activities in
   an expandable process view.
5. On completion, replace the headline with a concise outcome and mark the
   message `done`.
6. On error, keep any previous verified plan unchanged and mark the same
   message `error`.

The existing Resolve button should call the same conversation function with
`intent_hint: "resolve"`. It is a shortcut, not a separate result path.

## 10. Context filtering

The frontend may retain a single visible message list, but the backend should
construct workflow inputs explicitly.

### Author context

Include:

- user instructions that created or refined semantic tasks;
- final Author summaries needed to interpret follow-up references;
- current semantic actions after applying manual robot overrides.

Exclude:

- Resolver progress lines and raw reports;
- low-level tool names and arguments;
- compile warnings already represented in plan state;
- Explain answers that do not change semantic intent.

### Resolver context

Include:

- completed compiled plan;
- validated compile snapshot;
- current focus conflict and session ledger;
- previous Resolver tool results within the existing V2 bounds.

Exclude the general conversation transcript.

### Explain context

Use a read-only projection containing the semantic actions, current draft
summary, compile synchronization status, warnings, and conflicts. Do not expose
or mutate internal Resolver ledgers.

## 11. Concurrency, stale results, and cancellation

### 11.1 Revision guard

Every mutating request records `base_revision`. The frontend may apply its
terminal artifact only when:

```text
artifact.base_revision == current revision
and artifact.turn_id == active turn id
```

If the user edits the plan while a request is running, the result becomes
stale. The UI reports that the completed result was not applied.

### 11.2 One active mutating workflow

The first implementation should allow only one Author or Resolver mutation at
a time. Read-only Explain requests may also remain serialized initially to
keep message ordering simple.

### 11.3 Cancellation

Aborting the browser request only stops event consumption unless backend work
is cooperatively cancellable. Do not present a strong "Stop" guarantee until
the backend checks a cancellation token between LLM calls and compiles.

## 12. Failure behavior

- Routing failure: emit an `error` or `clarify` result without mutation.
- Author failure: keep the current actions and draft plan.
- Resolver failure before an accepted candidate: keep the current verified
  compile.
- Resolver failure after accepted candidates: return only the last verified
  partial plan and its matching compile, consistent with V2 today.
- Stream disconnect: do not apply an artifact that was not received completely.
- Snapshot mismatch/expiry: Resolver may perform the existing fresh initial
  compile fallback and must report that the snapshot was not reused.
- Terminal plan/compile hash mismatch: reject the result; never let the
  frontend adopt it.

## 13. Compatibility and migration

Keep these endpoints during migration:

- `POST /author`
- `POST /resolve_conflicts`

The unified endpoint should initially delegate to the same internal functions,
with event adapters added around them. This allows existing unit tests and
clients to remain valid.

Resolver selection remains controlled by `RESOLVER_VERSION`:

- default: V2;
- rollback: `$env:RESOLVER_VERSION="v1"`.

No part of the unified conversation layer should duplicate Resolver version
selection.

## 14. Recommended implementation phases

### Phase 0: contracts and tests

- Define intent, event, terminal-artifact, and revision schemas.
- Add router table tests for representative Chinese and English requests.
- Add tests proving Resolver eligibility cannot be bypassed by the classifier.
- Add tests proving only terminal artifacts mutate frontend state.

### Phase 1: unified frontend state, non-streaming adapter

- Replace `chatPending` and `resolvePending` decision paths with one turn state.
- Add one `sendConversation` entry point.
- Keep current endpoints internally and synthesize start/result/completion
  events from their final responses.
- Make the Resolve button pass `intent_hint: "resolve"`.

This phase validates routing and atomic commits without requiring streaming
transport changes.

### Phase 2: NDJSON transport and Author progress

- Add `POST /conversation/stream`.
- Connect Author's existing `on_event` callback to the common event adapter.
- Update one assistant bubble as events arrive.
- Preserve the old endpoints.

### Phase 3: Resolver progress

- Add an event sink to Resolver V2 orchestration boundaries.
- Emit focus, strategy, verification, accepted/rejected, deferred, and complete
  events.
- Do not change the scheduler, tools, session budgets, compile strategy, or
  candidate evaluation semantics.

### Phase 4: Explain workflow and UX refinement

- Add read-only plan/conflict explanations.
- Add expandable technical details.
- Add telemetry for routing decisions, time to first progress event, compile
  count, terminal outcome, and stale-result rejection.

### Deferred phase: compound pipelines

Only after the preceding phases are stable, discuss allowing a single request
to run:

```text
Author -> Compile -> Resolver
```

This requires a separate product decision for automatic compilation, failure
recovery, user confirmation, cost limits, and whether a resolved result is
committed automatically.

## 15. Acceptance criteria

1. A user can type "move both apples to the fridge" and the request routes to
   Author, updating one assistant message before returning a draft.
2. A user can type "解决现在的冲突" with an in-sync compiled plan and the
   request routes to Resolver V2.
3. The Resolve button produces the same route and message behavior as the text
   request.
4. A resolve request on a dirty or uncompiled draft does not call Resolver and
   explains the unmet prerequisite.
5. "把 apple_2 改到 sink" routes to Author even when conflicts exist.
6. "为什么 robot1 要等待" routes to Explain and does not mutate the plan.
7. Author and Resolver each update exactly one assistant message during a
   turn.
8. Resolver progress does not enter the next Author model context.
9. A V2 resolver terminal result adopts its returned compile without another
   `/compile_plan` request.
10. A stale terminal artifact cannot overwrite a newer manual edit.
11. A disconnected or failed stream leaves the last verified plan intact.
12. `RESOLVER_VERSION=v1` remains a functional rollback path.

## 16. Suggested implementation touchpoints

Likely files, subject to refactoring during implementation:

| area | current/new location |
|---|---|
| shared frontend message/event types | `frontend/src/plan/` or a new `frontend/src/conversation/` |
| unified frontend request/NDJSON reader | new `frontend/src/conversation/conversationClient.ts` |
| live message state and routing UI | `frontend/src/ScenePage.tsx` |
| author client compatibility | `frontend/src/plan/authorPlan.ts` |
| resolver client compatibility | `frontend/src/plan/resolveConflicts.ts` |
| unified HTTP route | `src/mujoco_skills/orchestrator/service.py` or a new conversation module |
| deterministic/LLM intent router | new `src/mujoco_skills/orchestrator/conversation.py` |
| Author event adapter | `src/mujoco_skills/orchestrator/authoring.py` plus conversation adapter |
| Resolver V2 event sink | Resolver V2 orchestration module, without scheduler changes |

## 17. Decisions for the next discussion

The following should be settled before implementation:

1. **First-release chaining:** confirm that one user turn routes to one workflow
   and does not automatically compile or resolve an Author result.
2. **Compile UX:** decide whether Compile remains only a button or also becomes
   an explicit chat intent such as "编译这个计划".
3. **Progress retention:** decide whether completed messages show the full
   activity timeline by default or collapse it behind "查看过程".
4. **Explain implementation:** decide whether deterministic templates are
   sufficient initially or whether a read-only LLM answer is required.
5. **Router model call:** decide whether the first release uses deterministic
   routing only, adding an LLM fallback later, or ships the hybrid router from
   the start.
6. **Editing while running:** decide whether the UI disables edits during a
   mutating turn or permits edits and relies on revision-based stale-result
   rejection.

Recommended defaults are: one workflow per turn, Compile remains explicit,
completed progress is collapsed, Explain starts deterministic, routing is
hybrid with deterministic fast paths, and plan editing is disabled during the
first implementation.

