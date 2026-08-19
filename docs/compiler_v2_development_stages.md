# Compiler V2 Overall Development Stages

> Status: implementation plan, 2026-08-15.
>
> Design source of truth: `docs/compiler_v2_contract.md`.

## Implementation status

Stages 0–6 were implemented on 2026-08-15. Stages 0–3 established the seam:

- coordination provenance and `prepare_compiler_v2_input()` exist, including
  manual-edit promotion and conservative legacy departure handling;
- V1 now calls the extracted `compile_one_step()` primitive and retains its
  existing memo/schedule path;
- `compile_plan_v2_no_repair()` performs chronological ready-frontier geometry
  compilation, reservation checking, and whole-step commit;
- `compile_plan(..., scheduler_mode="v2_no_repair")` exposes the Stage-3 core
  for tests and development only;
- synthetic scheduler/reservation tests and a real-MuJoCo cross-view baseline
  cover the current seam.

Stages 4–6 complete the first end-to-end version:

- `scheduler_mode="v2"` runs bounded deterministic navigation repair with the
  current go-away/yield, reroute, and add-after tools and records every
  accepted/rejected candidate in a repair ledger;
- a decomposed trailing `reset(retreat)` is recognized as a real departure
  anchor for temporal repair, while repair mutation remains navigation-only;
- `COMPILER_VERSION=v1|v2` selects the compiler at the skill-service boundary;
  V1 remains the default rollback path;
- the skill service preserves the existing schedule/track/completed response
  shape and adds `compiler_v2` metadata;
- compound turns using V2 perform one compile, bypass Resolver, nest the
  compiler-completed plan, and return the existing `turn_result` shape;
- an unrepaired conflict returns structured details atomically and commits no
  result;
- integration coverage includes a scripted natural-language Author turn that
  decomposes two robot tasks, runs the real MuJoCo Compiler V2 repair path,
  and returns a conflict-free frontend-compatible artifact.

Post-Stage-6 implementation work now also includes:

- topology-aware detour repair using computed parking points (the former
  go-away/yield terminology), including final-dwell/go-to-rest handling;
- ephemeral, editable detour geometry folded into the parent Task Plan task
  and exposed as a blue `navigate` operation in Step Plan;
- chronological assignment of an unlocked container close to the robot whose
  relevant placement finishes last, while preserving user-locked ownership;
- reaching-state per-step geometry memoization with completed-step aliases,
  bounded LRU storage, verify mode, and observable `hits/total` logging; and
- streamed committed-step compile progress through `/compile_plan_stream` and
  the compound-turn activity line.

Stages 7–8 remain rollout/retirement gates: V2 is implemented and selectable,
but the code-level default is still `COMPILER_VERSION=v1` and legacy rollback
has not been removed.

## 1. Goal

Replace the normal compound-turn pipeline:

```text
Author/edit/sync -> compile -> detect -> Resolver V2 -> recompile ... -> commit
```

with:

```text
Author/edit/sync
  -> prepare canonical step plan
  -> one incremental joint Compiler V2 run
  -> verified {plan, compile}
  -> commit
```

Compiler V2 compiles robot programs in chronological ready order, commits a
conflict-free prefix, and repairs only the current navigation using bounded
deterministic go-away, reroute, or wait candidates. It preserves the existing
`/compile_plan`, `CompileResponse`, `turn_result`, Gantt, playback, and scene-
overlay contracts.

## 2. Delivery principles

1. Keep Compiler V1 and the existing Resolver available behind a rollback
   switch until V2 passes the full compound-turn matrix.
2. Refactor current geometry and conflict code before changing behavior; do
   not build a second navigation compiler or collision implementation.
3. Land vertical, independently testable stages. Every stage must preserve the
   public response shape unless its exit criteria explicitly say otherwise.
4. A V2 success is conflict-free and needs no Resolver pass. A V2 failure is
   atomic and does not commit a partial user turn.
5. Optimize reservation lookup only after the reused current detector is
   correct in incremental mode.

## 3. Target ownership boundary

| Layer | Responsibility |
|---|---|
| Author/decompose | Semantic tasks, robot allocation, destination, ownership, authored cross-robot order |
| Compound input preparation | Preserve user edits; remove stale compiler-generated coordination |
| Compiler V2 | Geometry, timing, reservations, navigation-only coordination repair |
| Skill service | Validation, MuJoCo serialization, tracks, snapshots, public compile response |
| Compound tail | One V2 call, atomic success/failure, final nesting and reporting |
| Frontend | Adopt the unchanged `{plan, compile}` artifact |

## 4. Stage 0 — Baseline and characterization

### Work

- Freeze representative V1 fixtures and outputs for:
  - no conflict;
  - authored cross-robot `after`;
  - one-moving path conflict;
  - both-moving priority deadlock;
  - final-dwell blocking and `go_to_rest`;
  - shared-container open/place/close;
  - edited waypoint, standoff, placement, and destination;
  - compound author, edit, sync, and pure append.
- Record per scenario:
  - full `compile_plan` invocation count;
  - per-step compile count and memo hits;
  - wall time;
  - final plan, schedule, conflicts, tracks, and repair report.
- Add assertions for the cross-view identity invariant among nested plan,
  `completed`, schedule, and track URLs.

### Primary tests

- `tests/test_compound_turn.py`
- `tests/test_conversation_stream.py`
- `tests/test_detect_conflicts.py`
- `tests/test_conflict_tools.py`
- `tests/test_skill_service_compile_cache.py`
- `tests/test_compile_memo.py`

### Exit gate

The baseline suite can distinguish semantic/output regressions from acceptable
schedule or route changes, and reports compile counts for later V2 comparison.

## 5. Stage 1 — Coordination provenance and input preparation

This stage prevents old compiler conclusions from becoming accidental authored
constraints on edit, sync, or pure-append turns.

### Artifact-level provenance

Use provenance at the artifact being generated, not on an entire authored
step:

- inserted yield/rest steps: `source: "compiler_v2"` plus a repair kind;
- compiler-added dependency edges: retain the effective `after` array for
  compatibility and separately record exactly which edges were generated;
- derived `route`: remains derived and is ignored as an authored constraint on
  resubmission;
- authored `via_points`, placement, destination, and authored `after`: never
  receive compiler provenance.

The precise field names should be centralized in one helper module rather than
spread through tools and conversation code.

### Input preparation

Add one deterministic preparation function used by every V2 entry path:

```text
prepare_compiler_v2_input(plan, edit_deltas?)
```

It must:

1. flatten/copy without mutating caller state;
2. remove untouched V2-generated dependency edges and inserted coordination
   steps;
3. retain all authored/user-pinned spatial and semantic fields;
4. promote a generated artifact to user-owned when an explicit manual edit
   targets it;
5. handle legacy untagged Resolver artifacts conservatively, recognizing
   deterministic `#go_to_rest` ids where safe and otherwise preserving an
   ambiguous edge rather than deleting possible user intent;
6. return a preparation report for tests and compound-turn details.

### Integration points

- `orchestrator/plan_edits.py`: promote touched generated artifacts.
- `orchestrator/conversation.py`: call preparation after author/edit/sync plan
  selection and before topology validation.
- compiler public adapter: defensively prepare direct `/compile_plan` callers
  that did not pass through compound turn.

### Exit gate

Edit, sync, and pure append preserve explicit user modifications while stale,
untouched V2 repairs disappear and can be regenerated.

## 6. Stage 2 — Extract reusable single-step compilation

Refactor `skills/skill_generators.py::compile_plan` without changing V1 output.

### Work

- Extract environment initialization:
  - standoffs, manifest, rigs, shared world, robot contexts;
  - placement slots and container preprocessing;
  - memo generation and request profiling.
- Extract a `compile_one_step` operation around the existing `compile_robot`
  call, memo handling, track validation, context mutation, and facility-state
  effects.
- Return a candidate object containing compiled items, completed step data,
  pre/post contexts, shared-world state, and timing metadata.
- Make candidate state copyable so a failed repair can roll back without
  reconstructing the complete prefix.
- Keep the V1 `compile_plan` loop running through the extracted primitives and
  prove its public output is unchanged.

### Important constraint

`_container_dependency_order` currently combines dependency preprocessing and
compile ordering. Separate “derive effective container dependencies” from
“choose the next step” so V2 can retain the rules without inheriting V1's
single total compile order.

### Exit gate

All V1 compiler, memo, container, placement, and track tests pass through the
new single-step primitive with no intentional behavior change.

## 7. Stage 3 — Incremental scheduler and reservation correctness

Implement the V2 execution state without repair first.

### State

```text
per-robot cursor
per-robot compile context
shared world
completed step end times
committed items/completed steps
current time and ready frontier
reservation view
```

### Scheduling

- A robot's array cursor enforces same-robot order.
- A current step is ready when all cross-robot `after` targets have ended.
- Earliest start is the maximum of the preceding same-robot end and all
  cross-robot dependency ends.
- Choose the smallest earliest start; break exact ties by stable robot id.
- Compile and commit one whole step, then advance that cursor.

### Reservation implementation v1

Do not introduce a spatial database yet. Build a reservation view from
committed items using the existing base traces, occupancy segments, facility
information, and continuous-distance calculation.

When a navigation candidate is compiled:

1. assign its proposed absolute start;
2. compare only conflicts involving that navigation against committed moving
   and stationary occupancy;
3. accept if none are found;
4. otherwise return a structured first-conflict result without repairing it.

Non-navigation steps compile normally and reserve their stationary base pose
during manipulation/wait. They never invoke a motion repair.

### Tests

- No-conflict V2 plans match V1 semantic geometry and frontend contract.
- Cross-robot `after` gates readiness exactly at referenced-step end.
- Simultaneous readiness consistently lets `robot0` commit first.
- A navigation detects another robot moving, manipulating, waiting, and final-
  dwelling in its interval.
- Committed old-versus-old pairs are never rechecked as new conflicts.

### Exit gate

Every no-conflict fixture compiles successfully in one V2 pass, and every
conflict fixture stops deterministically at the expected current navigation.

## 8. Stage 4 — Local deterministic repair engine

Convert current Resolver tools from full-plan repair operations into bounded
candidate generators around the current uncommitted navigation.

### Candidate adapters

- `replan_path`: reuse current navgrid and conflict-window occupancy.
- wait/`add_after`: reuse topology-safe edge directions and departure anchors;
  target the current navigation.
- `insert_yield`: reuse deadlock proof and server-generated parking candidates;
  only modify/expand the current uncommitted navigation program.
- `insert_go_to`: reuse deterministic rest points and ids; append a terminal
  departure when a finished robot otherwise blocks forever.

`handoff_terminal_close` is excluded because it changes semantic ownership.

### Selection

```text
required applicable go-away
  -> reroute
  -> wait/add_after
```

Within a family:

```text
feasible -> least added duration -> least added path -> stable id
```

Each attempt runs on candidate state, checks topology and reservations, and
commits only when the current conflict disappears without changing the
committed prefix or authored constraints. Candidate keys and attempt caps make
the search finite.

### Output

- Apply accepted repair provenance from Stage 1.
- Emit a structured repair ledger: conflict, candidates considered, rejection
  reason, accepted repair, timing/path cost.
- If all candidates fail, raise/return a structured V2 compile failure. Never
  call Resolver as an internal fallback.

### Exit gate

The reroute, wait, yield, and go-to-rest fixture families converge without a
full-plan recompile and produce stable repair ledgers.

## 9. Stage 5 — Skill-service and public-output integration

### Work

- Add `COMPILER_VERSION=v1|v2` selection at the skill compiler boundary.
- Keep `POST /compile_plan {plan, retain_snapshot}` unchanged.
- Make V2 return the existing raw fields:

```text
items, conflicts, warnings, completed, rest_points
```

- Preserve schedule construction, generated-track writing, `track_url`,
  `compile_id`, compiler generation, and snapshot response fields.
- Add the V2 repair ledger as optional metadata without making the frontend
  depend on it for playback or rendering.
- Propagate structured V2 failures through the skill-service HTTP error and
  orchestrator client instead of flattening them into an opaque string.

### Exit gate

Existing frontend clients can call V1 or V2 through the same endpoint and
adopt successful results with no response-schema branch.

## 10. Stage 6 — Compound-turn cutover

Add a V2 compound branch while retaining the complete V1 branch for rollback.

### V2 flow

```text
select author/edit/sync input plan
  -> apply/preserve manual edits
  -> prepare_compiler_v2_input
  -> validate topology
  -> compile_fn(plan, retain_snapshot=True) exactly once
  -> final_flat = compile_result.completed
  -> final_plan = _nest_resolved_plan(final_flat)
  -> public_compile = _public_compile_result(compile_result, final_flat)
  -> emit verified turn_result
```

### Required changes

- Remove `delegable_conflicts` gating and the multi-pass
  `resolve_v2_core` loop from the V2 branch.
- Always nest the returned `completed` plan, even when no repair was needed;
  never return the pre-compile plan as the final plan.
- Preserve `actions`, append-promotion status, dropped edits, author message,
  plan history, and one-commit-per-turn behavior.
- On V2 failure, emit structured error details and do not emit a committing
  `turn_result`; the previous plan and actions remain current.
- Keep one `compiling` progress stage followed by `verified`. Intermediate
  repair streaming is deferred because `/compile_plan` is currently one
  synchronous HTTP call.

### Report compatibility

Adapt the compiler repair ledger into the current optional `report`, details,
summary, and warning slots so the frontend and Explain workflow do not require
an immediate state-model migration. Internally refer to it as a coordination
or compiler report even if legacy field names remain temporarily.

### Exit gate

Author, edit, sync, and pure-append compound turns each make one public compile
request, never invoke Resolver in V2 mode, and atomically commit the nested V2
completed plan.

## 11. Stage 7 — End-to-end compatibility and performance validation

### Functional matrix

For every representative scene/plan, verify:

- authored robot order and cross-robot `after` survive;
- user waypoints, standoff, placement point, destination, and locks survive;
- stale generated repairs are regenerated while promoted user edits survive;
- navigation never overlaps moving or stationary reservations;
- output plan, completed steps, schedule, Gantt ids, track URLs, and overlays
  agree;
- failure leaves the prior committed compound state untouched;
- sync/revert/history can resubmit the completed V2 plan.

### Performance gates

- One `/compile_plan` call per successful compound turn.
- No complete-plan compile inside candidate verification.
- Each step's expensive geometry compilation occurs once unless that same
  current navigation needs a bounded repair candidate.
- Repair attempt counts and total wall time are visible in the report.
- Target scenarios are materially faster than the recorded V1
  compile/resolve/recompile baseline.

### Regression suites

- Extend `test_compound_turn.py`, `test_conversation_stream.py`,
  `test_plan_edits.py`, `test_detect_conflicts.py`,
  `test_conflict_tools.py`, `test_compile_memo.py`, and
  `test_skill_service_compile_cache.py`.
- Add dedicated scheduler/reservation/repair-engine unit suites rather than
  putting every case through the live service.
- Add a small number of real MuJoCo end-to-end golden scenarios for tracks and
  scene playback.

### Exit gate

V2 passes the functional matrix, demonstrates the expected compile-count
reduction, and has no frontend adoption or compound atomicity regression.

## 12. Stage 8 — Rollout and legacy retirement

### Rollout

1. CI runs V1 and V2 fixture suites.
2. Local/development environments opt into `COMPILER_VERSION=v2`.
3. V2 becomes the compound-turn default while V1 remains an explicit rollback.
4. Observe structured failure reasons and performance telemetry.
5. Remove the rollback only after the agreed observation period.

### Deferred cleanup

After V2 is stable:

- remove the normal compound dependency on Resolver V2 and its snapshot-only
  plumbing;
- retain or separately retire explicit legacy resolve endpoints according to
  product needs;
- rename legacy `resolve_*` report fields in a versioned frontend migration if
  desired;
- replace the list-based reservation view with an indexed structure only if
  profiling demonstrates a need;
- reconsider fairness/makespan arbitration beyond the stable robot-id tie
  break.

### Final exit gate

Compiler V2 is the sole normal compound execution path, the old repeated full-
compile loop is unreachable by default, and rollback code can be deleted
without changing the public plan/compile contract.

## 13. Stage dependency summary

```text
Stage 0 baseline
  -> Stage 1 provenance/input preparation
  -> Stage 2 single-step extraction
  -> Stage 3 incremental scheduler/reservations
  -> Stage 4 local repair engine
  -> Stage 5 skill-service integration
  -> Stage 6 compound-turn cutover
  -> Stage 7 validation/performance
  -> Stage 8 rollout/retirement
```

Stages 1 and 2 may be developed independently, but both must be complete before
the compound V2 path is enabled. Stages 3 and 4 should remain separate so the
incremental scheduler can be proven correct before repair mutations are added.
