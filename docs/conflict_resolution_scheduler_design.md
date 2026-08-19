# Conflict-Resolution Scheduler V2

> Status: implemented; V2 is the default resolver.
>
> Updated: 2026-08-01.
>
> V2 replaces the V1 stateless, multi-conflict batch loop with a stateful,
> chronological resolver. Detection, the bounded low-level repair tools, and
> full compiler verification remain authoritative.

## 1. Why V2

The V1 product path is fully wired (`Compile` → warnings → `Resolve` → updated
plan/report), but its repair loop has the wrong unit of reasoning:

- every round sends all delegable conflicts to a stateless LLM call;
- the model can edit several conflicts in one batch;
- a successful compile is treated as progress even when the focused conflict
  did not improve;
- handoff, retirement, reroute, and ordering decisions are not remembered as
  durable commitments across rounds;
- conflict-local repairs can change earlier schedule decisions;
- terminal-close ownership can ping-pong between robots;
- failed or equivalent strategies can be proposed again after conflict ids or
  timing change.

V2 resolves these issues by making one compiler-produced conflict the only
editable focus of a round, always choosing the globally earliest unresolved
conflict, and preserving one LLM conversation plus an externally enforced
ledger across the complete run.

## 2. Scope and non-goals

### In scope

- Reuse the user's immediately preceding compile result as the initial Resolver
  observation, eliminating the redundant pre-round compile.
- Build a globally chronological queue of delegable conflicts.
- Execute at most one low-level repair tool for exactly one focus conflict per
  round.
- Recompile and re-sort the global queue after every executed tool.
- Preserve tool calls, tool results, accepted decisions, and failed attempts in
  a stateful LLM conversation.
- Maintain coordination-session ledgers for budgets and durable constraints;
  sessions are context containers, not repair units.
- Protect a logical causal/frozen prefix from regression.
- Delay `insert_go_to` and terminal-close ownership decisions until the
  chronological loop actually reaches the relevant final-dwell conflict.
- Produce a verified partial plan and accountable residual report when the loop
  reaches a cap.

### Explicitly deferred

- Incremental/suffix geometry compilation.
- Reusing frozen generated tracks between verification compiles.
- CBS-like joint spatiotemporal base planning.
- Generic LLM task reassignment.
- LLM-authored geometry or coordinates.

V2 initially performs a full fresh compile after every executed repair tool.
Correctness and observability come before compiler invalidation/caching work.

## 3. Contracts retained from V1

### 3.1 Deterministic compiler owns geometry

`compile_plan` remains the sole source of:

- absolute step timing;
- generated navigation/manipulation tracks;
- shared-container world state;
- exact continuous base-occupancy timelines;
- structured conflicts and human warnings;
- rest points and compiler-filled completed plans.

The LLM never writes paths, waypoints, poses, or collision geometry.

### 3.2 Conflict detection

The detector continues to build complete base occupancy over `[0, makespan]`:

- active compiled keyframes;
- stationary inter-step gaps;
- persistent final-pose occupancy after a robot's last step.

It analytically minimizes distance between overlapping piecewise-linear base
segments. A proximity conflict occurs below `STANDOFF_MIN_DIST` (currently
0.8 m). The base-center approximation remains intentional.

Conflict kinds remain:

- `path` and `facility`: automatically delegable;
- `object` and `placement`: human escalation.

Delegable conflicts retain the motion classification:

| class | meaning | permitted repair families |
|---|---|---|
| `both_dwelling` | both bases pinned | temporal ordering; constrained terminal handoff |
| `one_moving` | mover passes a pinned robot | reroute or temporal ordering |
| `both_moving` | corridor crossing | reroute or temporal ordering |

### 3.3 Bounded low-level tools

V2 keeps the existing generic tools. It does **not** add a fridge- or
shared-facility-specific high-level tool.

| tool | retained semantics |
|---|---|
| `replan_path(mover, avoid)` | deterministic priority reroute around the other robot's conflict-window occupancy |
| `add_after(step, after_step)` | add one compiler-verified cross-robot dependency |
| `insert_go_to(robot)` | append the deterministic `<robot>#go_to_rest` departure and reset for a final-dwell robot |
| `handoff_terminal_close(close_step, to_robot)` | move an eligible unlocked terminal close suffix to another robot |

Generic `reassign`, placement target changes, and destination changes remain
human/authoring operations.

For a dwelling party with a contiguous departure navigate, `add_after`
advertises two topology-safe temporal candidates. The aggressive
`departure_start` candidate anchors the waiter to the departure's direct
predecessor, making both movements ready together under the existing
finish-to-start dependency model. The conservative `departure_complete`
candidate anchors to the departure itself. V2 exposes aggressive candidates
first and only exposes conservative candidates after every aggressive option
for the focus has been attempted; every option still requires a full compiler
verification and rolls back on non-improvement.

## 4. Core terminology

### Focus conflict

The single globally earliest delegable conflict selected for the current round.
Every executable tool call in that round must reference its current conflict
fingerprint/id.

### Coordination session

A durable context/ledger shared by related conflicts, for example all conflicts
at `fridge`, or a connected corridor-crossing family. A session:

- stores history and accepted commitments;
- owns a six-attempt budget;
- prevents repeated or contradictory tools;
- groups the final report.

A session is **not** processed atomically and is **not** the repair unit. The
global queue may switch between sessions after any recompile.

### Round

One LLM decision cycle with:

1. one focus conflict;
2. at most one executed low-level tool;
3. one full fresh verification compile if a tool executes;
4. one structured tool result appended to the same conversation.

### Causal floor and frozen prefix

The causal floor is the earliest schedule point the selected repair may affect.
The logical frozen prefix contains accepted steps strictly before that floor
which are outside the repair's causal descendants.

V2 does not yet reuse their compiled tracks, but accepted repairs must not edit
their authored structure or introduce a new conflict before the causal floor.

## 5. End-to-end V2 flow

```text
User presses Compile
    ↓
skill_service returns result + compile_id
and stores a bounded, short-lived compile snapshot
    ↓
User presses Resolve with plan + compile_id
    ↓
Resolver validates plan hash / scene generation
and reuses that snapshot as Round-0 observation
    ↓
Deterministic code:
  - escalates object/placement conflicts to human
  - assigns path/facility conflicts to coordination sessions
  - builds a global chronological priority queue
    ↓
Start one stateful Resolver LLM conversation
with the global summary, session ledgers, and current plan topology
    ↓
Select the globally earliest unresolved conflict C
    ↓
Expose C as the only focus conflict
and expose only C's currently legal low-level tools/candidates
    ↓
LLM returns zero or more calls
    ↓
External validator executes at most one valid call for C
and returns rejected tool results for all extra calls
    ↓
Apply the selected tool to a candidate-plan copy
    ↓
Full fresh compile
    ↓
Improvement + causal-prefix verification
    ├── reject: roll back, update ledger, append failure result
    └── accept: commit plan/snapshot, update ledger and commitments
    ↓
Rebuild sessions as needed and globally re-sort all current conflicts
    ↓
If a newly created conflict is earlier, it becomes the next focus
even when it belongs to a different session
    ↓
Repeat until convergence, caps, or only human conflicts remain
    ↓
Return the last verified plan + session-grouped report
```

## 6. Initial compile snapshot reuse

The frontend has already compiled the exact plan shown to the user. V1 discards
that observation and compiles the same completed plan again at Resolver Round 1.
V2 removes only this duplicate.

### Required service contract

`POST /compile_plan` returns an opaque `compile_id` in addition to the existing
payload. `skill_service` stores a bounded TTL snapshot containing at least:

- canonical plan hash;
- scene/compiler generation;
- schedule, completed plan, conflicts, rest points;
- base occupancy timelines and scene reference needed by deterministic tools.

`POST /resolve_conflicts` receives:

```json
{
  "plan": {"robot0": [], "robot1": []},
  "compile_id": "c000123"
}
```

The orchestrator retrieves the snapshot from `skill_service` and verifies:

- snapshot exists and has not expired;
- its plan hash matches the supplied plan;
- its scene/compiler generation is current.

If validation fails, Resolve performs one fallback compile and records
`initial_snapshot_reused=false`. Snapshot reuse is not an exact-plan cache: it
is used once as the initial observation only. Every changed candidate plan is
freshly compiled.

## 7. Global chronological queue

After every accepted or rejected execution compile, V2 rebuilds the delegable
queue from the current verified snapshot.

Recommended deterministic order key:

```text
(
  conflict.window.start,
  simultaneous_severity,
  conflict.window.end,
  stable_fingerprint
)
```

Suggested simultaneous tie-break priority:

1. `both_dwelling` facility conflict;
2. `one_moving` conflict;
3. `both_moving` conflict;
4. stable fingerprint.

Final dwell is not moved ahead of earlier physical conflicts merely because it
is terminal. Its actual overlap window determines when it becomes focus.

The model sees a summary of every current conflict, but the engine exposes only
the focus conflict's executable candidates. Processing order is deterministic,
not an LLM decision.

## 8. Coordination-session registry

Sessions group history without forcing block-by-block processing.

Initial assignment rules:

- conflicts with a named facility use `facility:<facility>`;
- terminal close/final-dwell conflicts for that facility join the same session;
- path-only conflicts use a stable connected component over robot pair,
  overlapping windows, and shared semantic step groups;
- `object`/`placement` conflicts use human-only report groups.

The registry survives conflict-id renumbering. A suggested record is:

```json
{
  "session_id": "facility:fridge",
  "attempts_used": 2,
  "conflict_history": [],
  "tool_history": [],
  "failed_action_keys": [],
  "replan_keys": [],
  "insert_go_to_robot": null,
  "departure_robot": null,
  "close_owner": null,
  "handoff_direction": null,
  "resolved_until": 31.4,
  "status": "active"
}
```

After recompilation, conflicts may move, disappear, split, or appear. The global
queue is rebuilt, while session history is retained by facility/group lineage.
A session can be paused while another session owns the new globally earliest
conflict and resumed later with the same ledger and LLM history.

## 9. Stateful LLM conversation

V2 uses one message history for the complete Resolve run, reusing the provider
message/tool-result pattern already used by the authoring loop.

Initial context contains:

- all-conflict global summary;
- current chronological focus;
- schedule/dependency summary;
- session registry summaries;
- tool semantics and hard restrictions;
- frozen-prefix/causal-floor summary.

For every round:

1. append the updated focus payload;
2. call the provider with the existing message list;
3. append the assistant message including tool calls;
4. execute at most one valid call for the focus conflict;
5. append a tool result for every returned call (executed or rejected);
6. append the fresh compile delta before asking the model again.

The model may see all sessions for global understanding, but it cannot execute
a call for a non-focus conflict.

### Enforcing one tool call

Prompting is insufficient. If the model emits multiple calls:

- execute only the first valid call for the focus conflict;
- append `one_tool_per_round` rejection results for every additional call;
- count only an executed, compiler-verified candidate toward the session attempt
  budget;
- never silently drop a tool call, because the provider conversation requires a
  result for every tool-call id.

## 10. External tool ledger and monotonic restrictions

The LLM history improves reasoning; the deterministic ledger guarantees safety.

### `replan_path`

- Key: `(session_id, conflict_fingerprint, mover_step)`.
- At most one executed attempt per key.
- A failed reroute exhausts that key; later rounds must use a temporal repair.
- New conflicts/movers receive independent keys, even after global Round 1.

### `add_after`

- Must match a current topology-safe candidate.
- The exact dependency edge may be attempted only once after rejection and may
  be added at most once after acceptance.
- It may not edit a frozen step.
- Full compiler topology remains the final cycle check.

### `insert_go_to`

- Exposed for a qualifying final-dwell focus, or as a narrow server-generated
  departure prerequisite when the current facility visit has no safe
  `add_after` candidate solely because one participant will end at the
  facility without a later movement. The engine must prove that appending the
  departure and then adding the advertised wait edge is topology-safe.
- At most one successful departure robot per coordination session.
- Idempotence by `<robot>#go_to_rest` remains, but session exclusivity is
  enforced separately.
- A successful insertion records the session departure robot; the other robot
  cannot later be retired for the same terminal session.
- If the candidate compile fails and rolls back, the tool is recorded as a
  failed attempt, not as an accepted departure commitment.

### `handoff_terminal_close`

- Exposed only for a current terminal/final-dwell focus with an exact
  server-generated candidate.
- It is not selected speculatively at the start of a session.
- One successful handoff per session.
- Success locks `(from_robot, to_robot)` and `close_owner`; reverse handoff is
  forbidden for the remainder of the run.
- User-authored `robot_locked` remains absolute.

## 11. Candidate application and verification

Every executed call edits a deep candidate-plan copy. The current accepted plan
is untouched until verification succeeds.

### Ordinary improvement requirement

For `replan_path` and `add_after`, acceptance requires:

1. full compile succeeds;
2. the focus conflict disappears or strictly improves by a deterministic
   metric (overlap duration first, then minimum distance/severity);
3. no new `object` or `placement` conflict appears;
4. no accepted frozen step is structurally edited;
5. no new conflict appears before the selected tool's causal floor;
6. the compiler dependency graph is acyclic;
7. unrelated sessions are not made strictly more severe before the current
   focus time.

Otherwise the candidate rolls back and the structured failure enters both the
ledger and the LLM conversation.

### Structural-prerequisite exception

`insert_go_to` can be necessary without immediately removing the terminal
conflict. It may be accepted as monotonic structural progress only when:

- the focus is qualifying final dwell, or carries the exact verified
  facility-departure prerequisite described above;
- a previously absent deterministic departure anchor is created;
- session departure exclusivity is preserved;
- compile succeeds and all causal-prefix safety checks pass;
- the same structural prerequisite has not already been accepted.

The resulting rest-path conflict, if any, is inserted into the global queue and
may become the next focus.

`handoff_terminal_close` receives the analogous narrow exception only after the
departure decision is established and the focus still requires terminal close
ownership repair. It must lock one direction and cannot be accepted twice.

### Tool-result payload

Every executed tool result should include:

```json
{
  "accepted": true,
  "focus_fingerprint": "...",
  "session_id": "facility:fridge",
  "tool": "insert_go_to",
  "args": {"robot": "robot0"},
  "compile_succeeded": true,
  "focus_before": {},
  "focus_after": {},
  "resolved_fingerprints": [],
  "new_conflicts": [],
  "new_earliest_conflict": {},
  "makespan_before": 58.2,
  "makespan_after": 61.7,
  "causal_floor": 31.4,
  "frozen_prefix_preserved": true,
  "reason": "departure_anchor_created"
}
```

## 12. Causal floor and logical freezing

Chronological focus does not mean freezing everything before the conflict's
minimum-distance timestamp. A tool may affect an earlier part of the involved
step/program.

Tool-specific causal floors:

| tool | causal floor |
|---|---|
| `replan_path` | mover step start |
| `add_after` | delayed step start |
| `handoff_terminal_close` | first step of terminal close group |
| `insert_go_to` | departure robot's prior program completion/final-dwell start |

Example: a terminal conflict is observed at 45 s, but the finished robot began
final dwell at 30 s. `insert_go_to` creates a route starting at 30 s. A new path
conflict at 35 s is legal within the unfrozen suffix and is reinserted before
the 45 s terminal conflict. A conflict before 30 s is a regression and rejects
the candidate.

V2 logical freezing enforces:

- accepted tools cannot target frozen step ids;
- authored robot assignment, route constraints, and dependencies of frozen
  steps remain unchanged;
- no accepted repair introduces a conflict before its causal floor;
- future steps cannot be rescheduled ahead of the protected causal prefix.

The compiler still recompiles the whole plan in V2. Incremental track reuse is
a later optimization and must carry complete robot/shared-world/fixture state.

## 13. Terminal final-dwell policy

Terminal ownership is deliberately decided late, after earlier navigation and
placement conflicts have been repaired and the current verified schedule has a
stable completion order.

When a final-dwell conflict becomes focus:

1. inspect the current verified placement completion times for the shared
   facility;
2. respect explicit `robot_locked` intent first;
3. the earlier completed participant is the eligible departure robot;
4. `insert_go_to` is the only executable first departure repair and records the
   session commitment;
5. recompile and globally re-sort; resolve any earlier rest-path conflict first;
6. when the terminal conflict becomes focus again, the later completed
   participant is the eligible close owner;
7. if necessary, execute one constrained `handoff_terminal_close` and lock its
   direction;
8. subsequent `add_after` or reroute repairs remain ordinary one-tool rounds.

If user locks make this policy impossible, the session remains unresolved or is
escalated rather than overriding user intent.

## 14. Caps, convergence, and reporting

### Caps

- Admit at most three coordination sessions per Resolve run, in global
  chronological order. Conflicts belonging to later sessions are reported as
  `deferred` with reason `session_cap_reached` and remain for the next run.
- Maximum six executed verification attempts per coordination session.
- Rejected extra/non-focus LLM calls do not invoke compile; they are reported
  but do not consume a geometry attempt.
- The run-level verification ceiling is derived from the two limits above
  (`3 sessions * 6 attempts = 18`), not an independent scheduling budget.
- Tool application or compiler failures remain recorded and cannot be retried,
  but do not consume a verification attempt because no candidate was verified.

### Convergence

Stop when:

- no delegable conflict remains;
- every remaining delegable session is exhausted/unresolvable;
- the global cap is reached;
- only human `object`/`placement` conflicts remain.

The returned plan is always the last accepted, fully compiled plan. Failed
candidate edits never leak into the result.

### Report

Group the report by session while retaining chronological tool order:

```text
facility:fridge
- t=20.1 replan_path robot1/navigate_fridge: rejected (no alternate lane)
- t=20.1 add_after ...: accepted (+3.2 s)
- t=31.4 insert_go_to robot0: accepted (departure anchor created)
- t=35.0 replan_path robot0#go_to_rest: accepted
- t=45.0 handoff close robot0→robot1: accepted
- resolved 7 compiler warnings; session converged
```

Residual reasons should distinguish caps, exhausted tools, user locks, compiler
failure, no alternate route, dependency cycle, and human-only conflicts.

## 15. API and data changes

### Skill service

- Add `compile_id` and compiler/scene generation to `/compile_plan` responses.
- Add a bounded TTL snapshot registry; do not use it as an exact-plan cache.
- Add an internal snapshot lookup endpoint for the orchestrator.
- Preserve current compile lock and request instrumentation.

### Orchestrator service

- `/resolve_conflicts` accepts `compile_id` alongside the completed plan.
- Fetch/validate the initial snapshot; fallback compile only when invalid.
- Return `initial_snapshot_reused`, chronological rounds, session reports, and
  the last verified nested plan plus the public schedule/warnings/conflicts/
  completed payload from its final accepted verification compile.

### Frontend

- `usePlanCompile` retains the latest `compile_id` with `compiledPlan`.
- Resolve submits both `completed` and that `compile_id`.
- A plan edit invalidates the id for Resolve eligibility.
- Commit the plan returned by the Resolver and directly adopt its final compile
  payload, loading the already-generated track URLs without another
  `/compile_plan` request. This response handoff is not a snapshot or candidate
  cache; a subsequent Resolve without a frontend Compile uses normal fallback.
- Surface convergence/residual status in chat and warnings.

## 16. Implementation plan

V2 is the default resolver. Set `RESOLVER_VERSION=v1` to roll back while V1 is
retained during migration; the two loops do not share mutable state.

### Phase 0 — characterization and golden fixtures

1. Save representative compile snapshots for:
   - two apples to fridge with terminal close;
   - two mugs to cabinet/drawer;
   - two independent destinations;
   - both-moving corridor crossing;
   - one-moving/final-dwell crossing;
   - object/placement human escalation.
2. Add a scripted fake proposer for deterministic loop tests.
3. Record expected focus order and allowed tool calls.

Deliverable: tests that reproduce V1 handoff ping-pong, repeated proposals, and
the initial duplicate compile before V2 changes.

### Phase 1 — compile snapshot handshake

Files:

- `service/skill_service.py`
- `orchestrator/service.py`
- `frontend/src/plan/{compilePlan,usePlanCompile,resolveConflicts}.ts`
- `frontend/src/ScenePage.tsx`

Tasks:

1. Return/store `compile_id`, plan hash, and generation.
2. Implement TTL/LRU lookup and invalidation.
3. Send `compile_id` from the frontend Resolve request.
4. Validate the snapshot in the orchestrator with fallback compile.
5. Instrument `initial_snapshot_reused` and compile counts.

Acceptance: a normal Resolve performs no compile before its first LLM decision;
the first changed candidate still performs a fresh compile.

### Phase 2 — V2 state model, queue, and sessions

Add suggested modules:

- `orchestrator/resolver_state.py`
- `orchestrator/resolver_sessions.py`
- `orchestrator/resolver_v2.py`

Tasks:

1. Define `ResolutionState`, `SessionLedger`, `FocusConflict`, and round records.
2. Implement stable fingerprints and session lineage.
3. Implement deterministic global ordering and re-sort after compile.
4. Track causal floor and logical frozen step ids.
5. Keep `object`/`placement` outside the executable queue.

Acceptance: unit tests prove that one global earliest conflict is selected even
when session membership interleaves in time.

### Phase 3 — stateful single-call LLM loop

Files:

- `orchestrator/conflict_resolver.py`
- `orchestrator/loop.py` (reuse message conventions, not necessarily its
  multi-call execution behavior)
- `orchestrator/resolver_v2.py`

Tasks:

1. Replace the stateless `propose_repairs(payload)` round adapter in V2 with a
   persistent message list.
2. Build a focus payload containing global summary + session ledger + one full
   executable conflict.
3. Enforce at most one executed call and exact focus conflict id.
4. Append tool results for executed and extra/rejected calls.
5. Resume the same conversation after every compile.

Acceptance: provider tests demonstrate that the model receives its prior tool
call and compiler result and cannot execute a non-focus or second call.

### Phase 4 — deterministic ledger and verification gate

Files:

- `orchestrator/conflict_tools.py` (retain tool semantics)
- `orchestrator/resolver_state.py`
- `orchestrator/resolver_v2.py`
- `orchestrator/conflict_payload.py` (focus-candidate shaping)

Tasks:

1. Add per-tool action keys and attempt/exclusivity checks.
2. Compute tool-specific causal floors.
3. Apply every tool on a candidate-plan copy.
4. Compare pre/post focus metrics and conflict sets.
5. Add structural-prerequisite acceptance for first `insert_go_to` and eligible
   handoff only.
6. Roll back failed candidates and blacklist the action key.
7. Re-sort globally after every executed candidate.

Acceptance: compile success without focus improvement no longer commits an
ordinary repair; prerequisite insertions are accepted exactly once.

### Phase 5 — chronological terminal-close policy

Tasks:

1. Derive terminal facility placement completion order only when its
   final-dwell conflict becomes focus.
2. Restrict eligible `insert_go_to` to the earlier completed unlocked robot.
3. Lock one departure robot per session after acceptance.
4. Resolve newly introduced rest-path conflicts in global chronological order.
5. Restrict terminal handoff to the later completed eligible robot.
6. Lock close ownership and forbid reverse handoff.

Acceptance: the apple/fridge scenario never hands close ownership back, never
retires both robots, and resolves/restates every conflict in chronological
order.

### Phase 6 — product report and V1 removal decision

Tasks:

1. Extend response/report types with focus time, session, attempt, acceptance,
   makespan delta, causal floor, and residual reason.
2. Render chronological session-grouped accountability text.
3. Add backend/frontend telemetry for rounds and compile count.
4. Run V1/V2 side-by-side golden comparisons behind the feature flag.
5. Make V2 default only after acceptance scenarios pass repeatedly; then remove
   obsolete V1 batch/isolate-handoff logic in a separate cleanup change.

## 17. Test and acceptance matrix

### Queue and focus

- Globally earliest conflict wins across different sessions.
- Tie-break is deterministic.
- Exactly one conflict is executable per round.
- Exactly one tool call is executed per round.
- A new earlier conflict is reinserted at the queue head.

### Stateful context

- Prior tool calls and results appear in the next provider request.
- Paused session history is preserved when another session becomes earlier.
- Conflict id renumbering does not reset session history/tool budgets.

### Tool restrictions

- Reroute key executes once; failure forces a temporal alternative.
- Duplicate/rejected dependency edges are not retried.
- Only one departure robot can be accepted per terminal facility session.
- Successful handoff cannot reverse.
- `robot_locked` prevents automatic ownership changes.

### Verification and rollback

- Compile exception rolls back the candidate.
- Ordinary compile success without focus improvement rolls back.
- First valid departure-anchor insertion may commit as structural progress.
- Frozen authored steps cannot be targeted or regressed.
- New conflict before causal floor rejects the candidate.
- New conflict after causal floor is accepted into the global queue when all
  other acceptance conditions pass.

### Compile behavior

- Valid initial snapshot removes one duplicate compile.
- Invalid/expired snapshot triggers exactly one fallback compile.
- Every executed repair candidate performs one full fresh compile.
- Rejected extra/non-focus LLM calls perform no compile.

### End-to-end scenarios

- Two apples/fridge terminal close converges without handoff ping-pong.
- Two mugs/cabinet resolves or returns a precise exhausted-tool reason.
- Independent destinations keep parallelism and require no edits.
- Corridor reroute failure falls back once to temporal ordering.
- Object/placement conflicts remain human-only.
- Returned plan always equals the last verified compile, including partial
  convergence at a cap.

## 18. Key implementation files

- Detection/geometry: `src/mujoco_skills/skills/skill_generators.py`
- Compile service/snapshots: `src/mujoco_skills/service/skill_service.py`
- Existing V1 loop: `src/mujoco_skills/orchestrator/resolve_conflicts.py`
- Payload shaping: `src/mujoco_skills/orchestrator/conflict_payload.py`
- Deterministic tools: `src/mujoco_skills/orchestrator/conflict_tools.py`
- LLM adapter/provider: `src/mujoco_skills/orchestrator/conflict_resolver.py`,
  `orchestrator/providers/*`, `orchestrator/loop.py`
- Orchestrator HTTP: `src/mujoco_skills/orchestrator/service.py`
- Frontend compile/resolve: `frontend/src/plan/*`, `frontend/src/ScenePage.tsx`

## 19. Contribution framing

V2 remains neuro-symbolic multi-robot TAMP coordination:

- deterministic compilation owns geometry and verification;
- structured conflicts expose an accountable coordination surface;
- a stateful LLM chooses one bounded repair for one chronological conflict;
- deterministic queueing, ledgers, causal-prefix rules, and fresh compilation
  constrain every contribution;
- the final report explains accepted actions and residual limitations.

Do not describe the LLM as planning robot motion. It interprets verified
conflicts and chooses among bounded symbolic repairs.
