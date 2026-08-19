# Compiler V2 Boundary and Compatibility Contract

> Status: original boundary decision plus implemented extensions, 2026-08-15.
>
> Scope: freeze the data boundary before and after Compiler V2 and record the
> agreed incremental joint-compilation algorithm. Detailed implementation
> staging remains separate from this decision record.

## 0. Current implementation addendum

The core boundary below remains valid: Compiler V2 consumes decomposed
per-robot step programs and returns the existing completed-plan/schedule
contract. The implementation subsequently added these agreed extensions:

- Public terminology is **detour** rather than go-away/yield. A detour remains
  a compiler-generated navigation step. It is folded into its parent semantic
  task in Task Plan, shown as a blue `navigate` step in Step Plan, and rendered
  with the owning robot's route color.
- An edited detour route is stored only as an ephemeral override for the same
  deterministic conflict ID. It is regenerated while that conflict remains
  and discarded when the repair is no longer required.
- Unlocked refrigerator/drawer/cabinet closes are deliberately deferred. Once
  relevant placement completion times are known, the close is assigned to the
  robot whose relevant placement finishes last chronologically. A user-locked
  close is never reassigned. This is a narrow compiler scheduling rule, not a
  general license to rewrite semantic allocation.
- Compiler V2 has a process-local per-step geometry memo
  (`MJSKILL_COMPILE_MEMO=on|off|verify`, default `on`). Cache hits restore
  deep-copied post-step context/world state, but reservations, scheduling, and
  navigation conflict checks always run again.
- `/compile_plan_stream` reports committed-step progress for the compound UI as
  `Compiling the plan. (completed/total)`. Generated detours may increase the
  total during a run.

Where later sections use `go-away` or `yield`, read them as the internal or
historical name of the current detour repair. Where §7.4 excludes close-owner
handoff, the deferred-close rule above is the implemented, intentionally
bounded exception.

## 1. Decision summary

Compiler V2 will not consume semantic `AugmentedAction` objects directly and
will not introduce a new frontend result format.

Its logical input is the step-level plan produced after Author decomposition
and after any compound-turn plan preservation/manual-edit processing. Inside
the compiler this plan is canonicalized to one ordered step program per robot:

```text
{
  "robot0": [step, ...],
  "robot1": [step, ...]
}
```

Its public output remains the existing resolved compound-turn pair:

```text
{
  plan:    nested compiler-completed AuthoredPlan,
  compile: existing CompileResponse
}
```

This lets the current Gantt chart, playback, route/waypoint overlay, standoff
editor, placement editor, version history, and turn-result adoption continue
without a new frontend representation.

## 2. Representations and ownership

The current system has four distinct representations. They must not be
collapsed into one contract.

### 2.1 Semantic authoring state

`AugmentedAction[]` is the Author's persistent semantic working state. It
contains intent such as allocation, move destination, open/close ownership,
semantic ordering, and task identity.

The LLM may modify this state in response to user intent. It is not Compiler
V2's direct input.

### 2.2 Decomposed authored plan

`decompose()` expands every semantic action into a nested `AuthoredPlan`:

```json
{
  "tasks": [
    {
      "task": "move_apple",
      "robot": "robot0",
      "robot_locked": false,
      "steps": [
        {"id": "move_apple:s0", "group": "move_apple", "op": "navigate", "target": "apple"},
        {"id": "move_apple:s1", "group": "move_apple", "op": "pick", "object": "apple"},
        {"id": "move_apple:s2", "group": "move_apple", "op": "navigate", "target": "fridge"},
        {"id": "move_apple:s3", "group": "move_apple", "op": "place", "object": "apple", "dest": "fridge"},
        {"id": "move_apple:s4", "group": "move_apple", "op": "reset"}
      ]
    }
  ]
}
```

This is the compound-turn plan-level boundary. Manual edits also target this
step-level representation.

### 2.3 Canonical per-robot step programs

`flatten_tasks()` converts the nested plan into the form on which the compiler
actually operates:

```json
{
  "robot0": [
    {"id": "move_apple:s0", "group": "move_apple", "op": "navigate", "target": "apple"},
    {"id": "move_apple:s1", "group": "move_apple", "op": "pick", "object": "apple"}
  ],
  "robot1": []
}
```

The array order is the authoritative same-robot execution order. The canonical
Compiler V2 core consumes this representation.

### 2.4 Compiler-completed plan and schedule

Compilation fills geometry and execution results back onto copies of the
steps, including `route`, `standoff`, placement `at`, effective dependencies,
and any compiler-generated coordination steps. It also returns absolute timing
and track URLs through `CompileResponse.schedule`.

This is the source used by the frontend after a committed turn.

## 3. Current compound-turn flow before compilation

### 3.1 Natural-language turn

The current path is:

```text
current_actions
  -> authoring.parse_actions
  -> Author LLM mutates AugmentedAction[]
  -> decompose(actions)
  -> nested AuthoredPlan with stable step ids/groups
  -> optional pure-append promotion against the previous resolved plan
  -> semantic removals and/or pending manual-edit replay
  -> stale resolver departure cleanup after structural edits
  -> topology validation
  -> POST /compile_plan {plan, retain_snapshot}
```

Pure-append promotion may preserve already-compiled/resolved task blocks from a
previous turn. A non-pure-append author turn normally uses the freshly
decomposed plan.

### 3.2 Manual edit or sync turn

`intent_hint` values `edit` and `sync` skip Author and decomposition. They use
the previous committed plan directly:

```text
previous committed plan
  -> replay pending step-level edit deltas (edit only)
  -> remove stale departure conclusions after structural edits
  -> topology validation
  -> POST /compile_plan {plan, retain_snapshot}
```

This behavior is necessary because the committed plan can contain step-level
artifacts that semantic `AugmentedAction[]` cannot express.

### 3.3 Existing compiler entry

The skill service currently:

```text
validate_plan_topology(plan)
  -> compile_plan(scene, plan, ...)
  -> flatten_tasks(plan)
  -> _container_dependency_order(...)
  -> per-step geometry compilation
  -> schedule(items)
  -> detect_conflicts(items)
```

`compile_plan` accepts both nested `{"tasks": [...]}` and already-flat
`{"robot0": [...], "robot1": [...]}` plans. Compiler V2 should retain this
adapter compatibility at the public service boundary, while using only the
canonical flat representation internally.

## 4. Compiler V2 input contract

### 4.1 Boundary

The public `/compile_plan` request remains unchanged:

```json
{
  "plan": {"tasks": []},
  "retain_snapshot": true
}
```

The Compiler V2 core receives the canonical result of `flatten_tasks()`:

```text
PerRobotStepPlan := Record<robotName, CompletedOrAuthoredStep[]>
```

No new Author-to-compiler wire format is required.

### 4.2 Required identity and grouping fields

Every step must retain:

- globally stable `id`;
- owning robot, represented by the containing robot array;
- stable semantic `group`/task id;
- `op` and its operation-specific semantic fields;
- `robot_locked` where applicable.

Compiler-generated steps must also receive stable, deterministic ids and
groups so the Gantt, plan tree, completed plan, and schedule can join by id.

### 4.3 Program order and dependencies

The contract separates three sources of order:

1. **Same-robot program order** is represented only by array order. Explicit
   same-robot `after` edges are redundant and should be removed during
   canonicalization after validating that they agree with program order.
2. **Authored cross-robot order** remains an explicit step-level `after`
   dependency and is a hard input constraint.
3. **Compiler-inferred order** includes container topology and coordination
   decisions. It is derived inside Compiler V2 and must not be confused with a
   user-authored constraint.

The current container rules remain part of compiler preprocessing: Open gates
the destination-facing navigation/place operation, placements share the
required container state/order, and Close follows all relevant placement
completion steps.

### 4.4 Spatial fields

Compiler V2 must preserve the existing meanings:

| Field | Meaning at input | Compiler authority |
|---|---|---|
| `via_points` | authored navigation constraints | hard constraint; do not silently rewrite |
| `standoff` | authored override or prior compiler default | preserve an authored override; recompute only when not pinned |
| `at` | explicit placement point | hard placement constraint |
| `at_anchor` | placement distribution anchor | preserve and use for placement generation |
| `dest` | semantic placement facility | hard semantic constraint |
| `route` | prior compiler-derived full path | read-only derived data; never treat it as authored waypoints |

The existing navigation implementation already distinguishes `via_points`
from `route`: only `via_points` are fed back as route constraints.

### 4.5 Permitted compiler additions

Compiler V2 may add only coordination/execution artifacts, for example:

- effective cross-robot dependencies used to delay a step;
- a deterministic departure/`go_to_rest`/`go_away` step when needed;
- compiler-derived full routes and spatial defaults;
- explicit wait steps if the selected future algorithm chooses to expose a
  wait as a visible step rather than only as an idle schedule gap.

It must not silently change semantic allocation, object, destination,
open/close ownership, user-authored path constraints, or explicit placement.
Those decisions belong to Author or direct user editing before compilation.

## 5. Compiler V2 output contract

### 5.1 Raw skill compiler result

Compiler V2 retains the current raw return shape:

```python
{
    "items": items,
    "conflicts": conflicts,
    "warnings": warnings,
    "completed": completed,
    "rest_points": rest_points,
}
```

This lets the existing skill service continue to generate the public schedule,
write track files, and assign `track_url` values.

### 5.2 Existing public CompileResponse

The public compile payload remains:

```ts
type CompileResponse = {
  compile_id?: string;
  schedule: ScheduleEntry[];
  warnings: string[];
  conflicts: Conflict[];
  completed: Record<string, AuthoredStep[]>;
};
```

Each schedule entry must continue to provide:

```text
id, after, robot, label, start, duration, op,
facility, object, group, robot_locked, track_url
```

`completed` must contain the final per-robot step programs with all spatial
fields and compiler-generated coordination artifacts.

### 5.3 Existing compound-turn terminal artifact

Compiler V2 should directly produce the same terminal pair currently produced
after Resolver V2:

```python
final_flat = compile_result["completed"]
final_plan = _nest_resolved_plan(final_flat)
public_compile = _public_compile_result(compile_result, final_flat)
```

The compound turn then returns:

```json
{
  "kind": "turn_result",
  "plan": {"tasks": []},
  "compile": {
    "schedule": [],
    "warnings": [],
    "conflicts": [],
    "completed": {}
  },
  "actions": []
}
```

The final `plan` must be nested from `compile_result.completed`, not copied
from the pre-compile input. Compiler V2 may have added dependencies, departure
steps, routes, standoffs, or placement results that the frontend must be able
to inspect and edit on the next turn.

### 5.4 Cross-view consistency invariant

For every final step, these views must agree:

```text
turn_result.plan task step
    <-> compile.completed[robot] step
    <-> compile.schedule entry
    <-> generated track referenced by track_url
```

They must share the same stable step id, robot, group, operation, and effective
dependency identity. A compiler-generated step is not complete until it exists
consistently in all applicable views.

## 6. Existing frontend consumers preserved by this contract

No new frontend model is needed:

- `turn_result.plan` becomes `livePlan` and `draftPlan`, drives task grouping,
  selection, editing, history, and subsequent sync input.
- `compile.schedule` drives Gantt bars through `id`, `robot`, `label`, `start`,
  `duration`, `group`, `op`, and `facility`.
- `compile.schedule[].track_url` drives `SchedulePlayer` playback at the
  compiler-provided absolute start time.
- `compile.completed` drives scene overlays: navigate `route` and `standoff`,
  authored `via_points`, and placement `at`.
- `compile.conflicts[].steps` and `.window` drive exact Gantt warning tinting
  and conflict markers. A successful Compiler V2 normally returns no
  delegable path/facility conflicts; genuinely residual conflicts retain the
  current display path.

The existing `adoptCompileResult(plan, compile)` path must remain the adoption
mechanism, avoiding a redundant frontend `/compile_plan` call.

## 7. Agreed Compiler V2 algorithm

### 7.1 Execution state and readiness

Compiler V2 maintains one cursor per robot over its ordered step array. Same-
robot ordering is implicit in that cursor and is not rebuilt as explicit
dependency edges.

The only additional readiness gate is an authored or compiler-generated
cross-robot `after`: the current step is ready when every referenced step on
another robot has ended. Its earliest start is therefore:

```text
max(previous same-robot step end, all cross-robot after-step ends)
```

When multiple robots are ready at the same earliest time, the earlier start
wins. An exact tie is broken deterministically by robot id (`robot0` before
`robot1`). This initial deterministic policy is allowed to favor the lower id;
fairness or makespan-aware arbitration is a later optimization, not a V2
correctness requirement.

### 7.2 Incremental compile and commit loop

The compiler advances ready robot cursors in chronological order:

```text
select earliest ready step (stable robot-id tie break)
  -> compile the step with the existing operation compiler
  -> for navigation, query its candidate trajectory against reservations
  -> if safe, commit the whole step and its reservations
  -> if conflicting, generate and verify local navigation repairs
  -> commit the first accepted repaired result
  -> advance that robot cursor
```

The commit unit is a whole step. A committed prefix is immutable: a later
candidate may change the current uncommitted navigation and may insert
coordination navigation around it, but it may not rewrite an earlier committed
step. Consequently, conflict checking is incremental and never re-runs a
global old-versus-old conflict scan.

All operation compilers still run because durations, tracks, object state, and
robot poses are needed by the final artifact. Navigation is the only operation
class that triggers spatial conflict repair.

### 7.3 Reservation model

A reservation is the collision-query view of an already committed execution
interval, not a second frontend format. It can initially be derived directly
from the current compiled items/tracks and must identify at least:

```text
robot, step id, start, end, swept poses/trajectory, occupied facility if any
```

Reservations describe the dynamic compiled world, rather than a single
instantaneous MuJoCo snapshot. They include:

- swept occupancy while another robot navigates;
- the stationary robot footprint while it grasps, places, opens, closes,
  resets, or waits;
- facility occupancy where the current compiler models it;
- the static scene and current object/facility state used by navigation.

Only a newly compiled navigation actively queries for and repairs conflicts.
However, its query is against all relevant moving and stationary reservations.
Non-navigation steps therefore extend/reserve the robot's current pose even
though they do not themselves invoke a repair tool. This prevents a moving
robot from crossing a robot that is stationary during manipulation or wait.

The first implementation should reuse the current trajectory sampling,
robot-footprint, dwell, facility, and conflict calculations, restricted to
conflicts in which the current navigation candidate participates.

### 7.4 Navigation-only repair scope

Compiler V2 repairs execution geometry and timing only through navigation:

- `replan_path` changes the current navigation's derived route while
  preserving authored `via_points` and destination;
- wait/`add_after` delays the current navigation;
- `insert_yield` inserts temporary parking navigation, lets the winner pass,
  and then resumes the interrupted navigation;
- `insert_go_to` appends the deterministic terminal navigation
  `<robot>#go_to_rest` for a robot that otherwise remains in final dwell.

Grasp, place, open, close, and other semantic steps are never reassigned or
rewritten to repair motion. In particular, `handoff_terminal_close` crosses
the Author/compiler ownership boundary and is not part of the Compiler V2
motion-repair set. If ownership must change, Author or an explicit user edit
must do it before compilation.

Compiler-generated repairs are returned in `completed` and the nested final
plan: reroutes expose their resulting route/waypoints as today, waits expose
their effective coordination dependency/timing, and yield/rest navigation
steps receive stable ids and tracks. Generated coordination artifacts must be
distinguishable from authored constraints (for example with
`source: "compiler_v2"`) so a later structural edit can discard stale repairs
without discarding user intent.

### 7.5 Candidate generation and deterministic priority

The existing Resolver tool internals remain valuable and should be refactored
or wrapped as local deterministic candidate generators. Compiler V2 reuses:

- topology-safe `add_after` candidates;
- navigation rerouting against conflict-window occupancy;
- spatial priority-deadlock analysis and exact yield candidates;
- deterministic rest points and `go_to_rest` construction;
- apply-on-copy, verify, accept-or-rollback semantics.

It does not retain the LLM tool-selection session, global conflict queue, or
the full-plan compile/resolve/recompile loop.

Repair priority is conditional on applicability:

```text
required go-away -> reroute -> wait/add_after
```

`required go-away` is selected immediately only when the engine has proved one
of these structural cases:

- neither robot priority ordering can pass, so an advertised `insert_yield`
  candidate is required; or
- a final-dwell robot will never vacate the blocking pose, so
  `insert_go_to`/`go_to_rest` is required.

Ordinary path crossings do not generate gratuitous go-away moves. For them,
the current preference remains reroute before temporal serialization. Facility
ordering uses an applicable wait/departure candidate and does not invent
geometry.

Within one repair family, candidates are ordered deterministically by:

```text
feasible -> least added duration -> least added path length -> stable id
```

Every candidate is checked on a state copy against dependency topology,
authored spatial constraints, and current reservations. It is committed only
after the current conflict is removed and the committed prefix remains valid.

### 7.6 Failure semantics

If every applicable go-away, reroute, and wait candidate for the current
navigation fails, Compiler V2 stops with a structured compile failure for that
step. It may return the verified prefix and residual diagnostic data, but it
must not fall back to the old full-plan Resolver loop. Candidate enumeration
must be finite and deterministic.

## 8. Minimum-change implementation seam

The intended engineering boundary is narrow:

### Keep unchanged

- Author tools and semantic action model;
- `decompose()` templates;
- compound-turn edit replay and stale-revision handling;
- `/compile_plan` request and public response types;
- track file loading and playback;
- Gantt and scene overlays;
- `turn_result` adoption, history, and revert.

### Replace or extend

- the compiler core after `flatten_tasks()` and container dependency
  preprocessing;
- the current separate `schedule(items) -> detect_conflicts(items) ->
  Resolver V2` normal path;
- compound-turn tail selection so a successful Compiler V2 result is nested
  and committed directly.

The first implementation intentionally avoids general backtracking. It commits
the earliest verified step and searches only the bounded local repair
candidates described above.

## 9. Acceptance criteria for the contract

1. A current nested `AuthoredPlan` can be passed to Compiler V2 unchanged.
2. A current flat completed plan can still be accepted on sync/migration paths.
3. Same-robot execution order is determined by the per-robot step array.
4. Authored cross-robot dependencies and user spatial edits survive exactly.
5. Compiler-derived `route` is not promoted into an authored path constraint.
6. Compiler-generated dependencies/departures have stable ids and appear in
   the final nested plan, completed plan, schedule, and tracks as applicable.
7. The existing frontend can adopt the returned `{plan, compile}` without
   recompiling and without new Gantt/overlay data types.
8. The final schedule, completed plan, nested plan, and track files describe
   one internally consistent execution artifact.
9. Same-time readiness is resolved deterministically by robot id.
10. Every committed navigation is conflict-free against all earlier moving and
    stationary reservations over its relevant execution interval.
11. Motion repair changes only navigation geometry/timing or inserts explicit
    yield/rest navigation; it never changes semantic ownership or destination.
12. A successful compile requires no subsequent Resolver pass.

## 10. Relevant current implementation anchors

- Compound Author/decompose stage:
  `src/mujoco_skills/orchestrator/conversation.py::_run_author_stage`
- Compound plan preservation/edit path:
  `src/mujoco_skills/orchestrator/conversation.py::stream_compound_turn`
- Step decomposition:
  `src/mujoco_skills/orchestrator/decompose.py::decompose`
- Public compile adapter:
  `src/mujoco_skills/service/skill_service.py`
- Nested-to-flat normalization:
  `src/mujoco_skills/skills/skill_generators.py::flatten_tasks`
- Container dependency preprocessing and compiler:
  `src/mujoco_skills/skills/skill_generators.py::_container_dependency_order`
  and `compile_plan`
- Stage-1 coordination provenance/input preparation:
  `src/mujoco_skills/orchestrator/compiler_v2_plan.py`
- Stage-2/3 single-step and no-repair incremental core:
  `src/mujoco_skills/skills/skill_generators.py::compile_one_step`,
  `incremental_schedule_no_repair`, and `compile_plan_v2_no_repair`
- Resolved-plan nesting/public shaping to preserve:
  `src/mujoco_skills/orchestrator/service.py::_nest_resolved_plan` and
  `_public_compile_result`
- Frontend contract/adoption:
  `frontend/src/plan/planTypes.ts`, `frontend/src/plan/usePlanCompile.ts`, and
  `frontend/src/ScenePage.tsx`
