# System Features Overview

> Current implementation reference, updated 2026-08-15.
>
> This is the system-level source of truth for the implemented Author →
> Compiler V2 workflow. Historical rationale and intermediate designs remain in
> `compound_turn_integration_spec.md`, `compiler_v2_contract.md`, and
> `compiler_v2_development_stages.md`.

## 1. System at a glance

The system is a browser-based co-authoring workspace for building and refining
multi-robot plans in MuJoCo. A user can author a plan in natural language,
inspect it in task and step timelines, directly edit allocation/order/routes/
placements, compile it into replayable robot skills, and continue revising the
same persistent plan over multiple turns.

The current implementation has two major planning components:

- **Author** translates user intent into a persistent semantic plan and applies
  local additions or revisions without regenerating unrelated intent.
- **Compiler V2** consumes the decomposed step plan and incrementally produces
  one jointly scheduled, conflict-repaired execution artifact. It replaces the
  normal V1 `compile → conflicts → Resolver → recompile` loop.

Execution stitches deterministic skill tracks together. Compiler V2 reasons
over those tracks, sampled robot footprints, operation dwell, shared facility
state, and explicit dependencies; it is not a general closed-loop task-and-
motion planner.

## 2. Current data model

The implementation deliberately keeps four representations separate.

### 2.1 Semantic Author state

`AugmentedAction[]` is the persistent intent-level working state. Actions carry
stable IDs and fields such as operation, robot, object, destination, facility,
and semantic ordering. The Author may change these fields when the user asks
for an allocation, destination, or ordering revision.

### 2.2 Decomposed authored plan

Deterministic `decompose()` expands each semantic action into a task containing
step operations. Typical templates are:

```text
move  = navigate(object) → pick → navigate(destination) → place → reset
open  = navigate(facility) → open → reset
close = reset → navigate(facility) → close → reset
go_to = navigate(target/waypoints)
```

Each step retains a stable `id` and the semantic action ID in `group`. This
nested `AuthoredPlan` is also the boundary used by manual edits and the public
compile service.

### 2.3 Canonical per-robot step programs

Before compilation, `flatten_tasks()` canonicalizes the nested plan to ordered
lanes:

```text
{
  robot0: [step, ...],
  robot1: [step, ...]
}
```

Array order is the authoritative same-robot order. Only cross-robot `after`
constraints need to remain explicit; redundant same-robot `after` constraints
are not required to preserve the lane program.

### 2.4 Compiler-completed artifact

Compiler V2 returns the same public shapes already consumed by the frontend:

```text
turn_result.plan       nested completed plan
compile.completed      final per-robot step programs
compile.schedule       absolute timing and track URLs
compile.conflicts      residual diagnostics, normally empty on success
compile.warnings       non-fatal diagnostics
```

These views share stable step IDs and describe the same execution. Completed
steps include derived routes, standoffs, placements, effective dependencies,
and any compiler-generated coordination navigation.

## 3. Author and compound-turn workflow

### 3.1 Natural-language mutation

A mutating language turn follows this path:

```text
user instruction + optional scene/task references
  → classifier selects the mutation path
  → Author applies tools to current AugmentedAction[]
  → deterministic augment/validation
  → decompose full semantic state into step tasks
  → preserve eligible prior compiled work for a pure append
  → prepare the step plan for Compiler V2
  → compile once with incremental repair
  → nest compile.completed and commit one turn_result
```

The Author works from authoritative current state. Unmentioned actions retain
their IDs, allocation, order, destination, and user-authored spatial choices.
Scene references identify objects/positions; task references resolve stable
semantic IDs against server state rather than trusting browser display data.

### 3.2 Pure-append continuity

When new semantic actions have the prior action list as an exact unchanged
prefix, compound turn promotes the previous completed plan and appends only
the newly decomposed task blocks. It removes obsolete terminal departure steps
from robots receiving new work and invalidates placement results when a changed
slot group requires redistribution.

If the edit is not a safe pure append, the system recomputes the step plan from
the current semantic state rather than grafting incompatible compiled output.

### 3.3 Manual edits and sync

Structured frontend edits bypass natural-language interpretation. They replay
against the latest committed step plan and then enter the same Compiler V2
preparation/compile/commit tail. Supported edits include task allocation and
ordering, navigation waypoints/standoffs, and placement positions.

Read-only explanation requests use a separate path and do not mutate or
compile the plan.

## 4. Compiler V2

### 4.1 Boundary and preserved intent

The public `/compile_plan` request remains `{plan, retain_snapshot}`. Compiler
V2 consumes the canonical per-robot step programs internally and preserves:

- user-selected robot allocation and destination;
- authored navigation `via_points` and standoff overrides;
- explicit placement `at`/anchor data;
- per-robot step order; and
- authored cross-robot `after` constraints.

Derived `route` data is compiler output, not an authored waypoint constraint.
Compiler-generated repair data is marked with `source: "compiler_v2"` so it
can be distinguished from user intent on a later turn.

### 4.2 Incremental joint compilation

Compiler V2 maintains one cursor and execution context per robot. A current
step is ready after its previous same-robot step and all cross-robot `after`
predecessors finish. The ready step with the earliest start commits first; an
exact tie is resolved deterministically by robot ID (`robot0` first).

For every step, the existing operation compiler still produces duration,
track, final robot pose, object state, and facility state. The step is then
committed to an immutable prefix and its execution interval becomes a
reservation. Later steps compile against the state and reservations produced
by that prefix.

### 4.3 Reservations and conflict scope

Reservations are internal collision-query data, not a frontend format. They
cover:

- swept robot occupancy during navigation;
- stationary robot occupancy during pick/place/open/close/reset/wait;
- relevant shared-facility occupancy and state; and
- the time interval and identity of the committed step.

Only a newly compiled **navigate** step initiates conflict detection and local
repair. Its candidate route is checked against the static scene and relevant
moving or stationary reservations over the navigation interval. Non-navigation
steps still reserve their robot pose, preventing another robot from passing
through a robot that is manipulating or waiting.

### 4.4 Deterministic local repair

Compiler V2 reuses the proven V1/Resolver conflict primitives without running
an LLM resolver session or recompiling the full plan after every attempt. It
generates bounded candidates, applies each to a copy, verifies it, and commits
the first accepted result.

The applicable repair families are:

1. **Detour** when topology/priority analysis proves a robot must temporarily
   vacate a blocking region, including final-dwell cases.
2. **Reroute** the current navigation while preserving authored via points and
   destination.
3. **Wait/add-after** when temporal serialization is topology-safe.

Dependency-cycle detection happens before accepting a wait. Structural
priority deadlocks use the same topology analysis as the former resolver and
select a detour instead of discovering the cycle only after adding an edge.
If all bounded candidates fail, compilation stops with a structured failure;
it does not fall back to the legacy full Resolver loop.

### 4.5 Detour semantics

`detour` is the user-facing name for the former go-away/yield repair. Its
parking point is computed from the conflict geometry, free space, and the
robot's resume goal; it is not a fixed rest coordinate. A terminal
`go_to_rest` remains a distinct departure operation.

A detour is a real generated navigation step with its own route and track:

- in **Task Plan**, it is folded into the parent semantic task so the repair
  does not create an overlapping standalone task bar;
- in **Step Plan**, it appears as a blue `navigate` step;
- its scene route uses the owning robot's route color; and
- the route is editable, but the edit is an ephemeral override tied to the
  same deterministic conflict ID. If a later compile no longer needs that
  conflict repair, both detour and override disappear.

### 4.6 Deferred container close ownership

For an unlocked close of a refrigerator, drawer, or cabinet, Compiler V2
defers ownership until the relevant placement/reset steps have actual compiled
end times. It assigns the close to the robot whose relevant placement finishes
last chronologically and makes the close follow all required placements.

An explicitly user-assigned close has `robot_locked=true` and is never moved by
this rule. Assignment decisions are exposed in
`compiler_v2.close_owner_assignments` for diagnostics.

## 5. Compile memoization and progress

### 5.1 Per-step memo cache

Compiler V2 memoizes geometry compilation, not scheduling or repair decisions.
The key contains compiler generation, the exact reaching robot context, shared
world state, current canonical step, and its successor. Cached entries include
compiled items and deep-copied post-step context/world state. An alias keyed by
the compiler-completed form lets a subsequent compound append reuse the prior
turn's results.

Reservations are always rebuilt and every navigation is rechecked, including
on a geometry-cache hit. Consequently a hit cannot bypass new cross-robot
timing or conflict conditions.

Configuration:

```text
MJSKILL_COMPILE_MEMO=on       default; use the bounded process-local LRU
MJSKILL_COMPILE_MEMO=off      disable cache
MJSKILL_COMPILE_MEMO=verify   recompute hits and compare cached results
MJSKILL_COMPILE_MEMO_MAX_ENTRIES=512
```

The compile result reports `compiler_v2.memo` with mode, hits, misses, and
uncacheable counts. The skill-service completion log prints `hits/total` and a
percentage so production hit rate is visible.

### 5.2 Step-wise progress

`/compile_plan_stream` emits NDJSON progress while Compiler V2 commits steps.
The compound-turn UI displays:

```text
Compiling the plan. (completed/total)
```

The total can grow when compilation inserts a generated detour. The terminal
result still arrives as one atomic `turn_result`; progress events do not expose
or commit a partially compiled plan.

## 6. Frontend plan and scene views

- **Task Plan** groups execution by stable semantic action ID. Compiler detours
  are projected into their parent task.
- **Step Plan** shows compact operation labels: `navigate`, `pick`, `place`,
  plus `open <facility>` and `close <facility>`. Detours are blue navigate bars;
  open/close and `go to rest` remain support operations.
- **Scene overlays** use `compile.completed` for routes, authored waypoints,
  standoffs, detours, and placement locations.
- **Playback** uses absolute schedule timing and per-step `track_url` values.
- **Linked editing** maps timeline/scene selection back to stable step and
  semantic group IDs.
- **History/Revert** restores the previous committed plan. Read-only questions
  do not create history entries.

The frontend adopts the returned `{plan, compile}` directly and does not issue
a redundant second compile.

## 7. Runtime selection

The implemented paths are selected independently:

```text
COMPOUND_TURN=1       use the unified persistent Author/compile turn
COMPILER_VERSION=v2  use incremental joint Compiler V2
```

At code level, an unset `COMPILER_VERSION` still selects `v1`; deployments that
intend to use the current V2 workflow must set both variables. Memoization is
on by default once Compiler V2 is selected. Changes require restarting the
orchestrator and skill service.

## 8. Scope and invariants

- Same-robot ordering comes from lane order; explicit `after` is required only
  for cross-robot precedence.
- Compiler V2 may change execution geometry/timing through generated repair,
  but not semantic objects, destinations, or protected assignments.
- A successful result keeps nested plan, completed lanes, schedule, and tracks
  consistent by stable step ID.
- Generated detours are inspectable and editable execution artifacts, not
  durable semantic tasks.
- Remaining unsupported conflicts fail explicitly rather than silently
  changing user intent.
- Replay demonstrates the compiled skill sequence; it does not claim robustness
  to arbitrary physical disturbances.

For field-level input/output details and the full algorithm invariants, see
[`compiler_v2_contract.md`](compiler_v2_contract.md). For the compound-turn
history and implementation decisions, see
[`compound_turn_integration_spec.md`](compound_turn_integration_spec.md).
