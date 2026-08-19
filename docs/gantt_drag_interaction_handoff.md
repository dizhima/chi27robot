# Gantt task-drag interaction handoff

## Status

**Problem 2 (cross-robot `after`) is fixed and confirmed by the user.** Root
cause was a discarded edit, not a hit-testing problem; see "Cross-robot `after`:
root cause and fix" below.

**Problem 1 (origin-ghost overlap) is fixed and confirmed by the user.** It was a
box-model asymmetry between the ghost and a real bar, not a scheduling or
stacking problem. See "Ghost overlap: measured root cause".

Do not assume the passing unit tests prove these interactions work in the real
app. The suite now covers the pointer gesture in jsdom, but jsdom performs no
layout, so nothing in it can reproduce a rendered-geometry failure.

## Required final interaction

Task view separates its two outcomes by a **modifier key**, not by competing
hit areas. This replaces the earlier endpoint-proximity design: a single
distance threshold either consumed the gaps that insert needs (failed approach
3) or was too small to hit at all, and `5%` of total schedule time changes size
as the timeline grows.

| Gesture | Required edit | Robot changes? | Origin ghost? |
| --- | --- | --- | --- |
| Plain drag within the source lane | Reorder the whole task into the dropped slot | No | No |
| Plain drag onto the other robot's lane | Insert/reassign the whole task into that robot's order | Yes | Yes |
| **Shift**-drag onto a task on the other robot's lane | Add an explicit `after` dependency on the target task | No | No |

The slot for a plain drag is chosen from target-task midpoints, so every point
on the lane resolves to a slot and the insert caret always shows where the task
will land. A Shift-drag accepts the whole target bar as its hit area, which is
now safe because insert no longer competes for it. Shift is polled on every
pointer move and on `keydown`/`keyup`, so pressing or releasing it re-classifies
the drop without further pointer movement.

Vertical travel counts toward the drag threshold. Only `|dx|` was measured
before, so dragging a task straight down into the other lane — the most natural
reassignment gesture — registered as a click and did nothing.

Additional requirements:

- Only a cross-robot insert/reassignment produces a ghost.
- A same-robot reorder must never produce a ghost.
- For a cross-robot insert, the source lane must not compact/fill the old slot.
  The old slot remains occupied visually by the ghost.
- The destination lane may shift its suffix to make room for the inserted task.
- A cross-robot `after` leaves the dragged task on its original robot.
- Step view's original cross-robot `after` interaction remains available.
- An `after` drop must be visible immediately: the waiting task's left edge is
  pulled to the anchor task's right edge, and both bars are outlined. Leaving
  every bar in place was tried and rejected — a successful dependency edit was
  then indistinguishable from a dropped one.
- While a drag edit is unsynced, Step view should use the last valid compiled
  schedule instead of recomputing the dependency fixpoint. This avoids the
  previously observed 1000+ second preview cycle.
- Step labels must remain the short presentation labels (`navigate`, `pick`,
  `place`, and compact open/close labels), not raw compiler step IDs.
- Sync replaces all projections with the newly compiled real schedule.

## Cross-robot `after`: root cause and fix

### Root cause (confirmed by reading the code, not inferred)

The drop submitted **compiled** step keys, but `setStepAfter()` only accepts
**authored** step ids:

```ts
const known = new Set<string>();
for (const task of plan.tasks) for (const s of task.steps) if (s.id) known.add(s.id);
const next = [...new Set(afterIds)].filter((a) => a !== stepId && known.has(a));
const cur = step.after ?? [];
if (cur.length === next.length && cur.every((a, i) => a === next[i])) return plan;
```

`aggregateToTaskBars()` sets `endStepKey` to whichever member finishes last, and
that member is frequently compiler-generated. Per the compiler, none of these
ids exist in any authored plan:

| Kind | Generated id |
| --- | --- |
| geometry reposition leg | `{robot}#reposition{order}` |
| detour / go-away | `{blockedStepId}#detour_{occupyingRobot}` |
| deadlock yield | `{yieldingStepId}#yield` |
| terminal departure | `{robot}#go_to_rest`, `{robot}#go_to_rest:reset` |

So the target was filtered out, `next` became `[]`, `cur` was already `[]`, and
the plan was returned unchanged — while `editDraft()` queued the delta anyway.
That is exactly the reported "the edited count changes but nothing happens".
The same failure hits the dragged task's own `d.stepId` whenever its first
non-detour member is a generated step.

### Fix

Task view now dispatches a **task-level** dependency in semantic ids and lets
the plan owner resolve them:

- `GanttPanel` gained `onSetTaskAfter(actionId, afterActionId)`. It no longer
  passes compiled keys for this gesture at all.
- `planEdits.taskStepBounds(plan, actionId)` returns a task's first and last
  authored step ids, or null.
- `ScenePage.handleSetTaskAfter()` resolves both ends against `draftPlan` and
  queues `set_step_after` only when both resolve: the dragged task's first
  authored step waits for the target task's last authored step. An
  unresolvable id bails out instead of queueing a delta that `setStepAfter`
  would silently discard.

Dependency targets are still filtered by `draggableTaskIds`, which is the right
predicate: it is the set of semantic action ids, so it correctly excludes
compiler-only groups (`#yield`, `#go_to_rest`) that cannot be authored anyway.

`editDraft()` still queues whatever delta it is handed even when the transform
returns the same plan. That was left alone deliberately — detecting the no-op
needs the pre-edit plan read synchronously, and `editDraft` uses the functional
`setDraftPlan` form specifically so several edits in one tick compose. The
guard now lives at the call site instead. If a future change wants the general
guard, add a `draftPlanRef` mirror first.

## Current implementation

### Task projection and ghost

File: `frontend/src/plan/ganttModel.ts`

`projectTaskMoveBars()` replays pending `move_task` edits over task bars derived
from the last compiled schedule.

- Same-robot edits reorder and repack that lane, with no ghost.
- Cross-robot edits remove the task from the source lane without moving the
  remaining source bars.
- The moved task is inserted into the destination lane and destination suffix
  bars are shifted by its duration.
- The first compiled origin bar is returned in `ghostBars` when the final robot
  differs from the origin robot.

`sourceRobot` is reconstructed in `frontend/src/ScenePage.tsx` by starting from
`livePlan.tasks` and replaying pending move edits in order.

### Task dependency projection

`projectTaskAfterBars()` (same file) makes an unsynced `after` visible:

- the waiting task's start moves to `max(anchor end, its own lane predecessor's
  end)`, so its left edge meets the anchor's right edge;
- the waiter's lane is then cascaded in its **pre-shift order**, which is that
  robot's program order. Re-sorting by the new start times instead lets a later
  task overtake the delayed one, which a serial robot cannot do;
- a task already starting after its anchor keeps its position, because a
  dependency can only delay work, never pull it earlier;
- both ends are tagged `pendingAfter: "waiter" | "anchor"` for the outlines;
- durations stay at their last compiled values, and neither robot changes, so
  this projection never produces a ghost.

`ScenePage` resolves the pending `set_step_after` deltas from step ids back up
to semantic task ids through `draftPlan`, because the delta is authored at step
level while the task Gantt draws tasks. Move projection runs first: an `after`
must delay a task where a pending reassignment actually put it, not where it was
compiled.

### Rendering

Files:

- `frontend/src/plan/GanttPanel.tsx`
- `frontend/src/style.css`

Ghost bars are rendered as a separate set of absolutely positioned frames before
normal bars. The ghost has `is-drag-origin`; it is dashed, transparent,
non-interactive, and assigned `z-index: 0`, against `z-index: 1` for normal bar
frames.

The stacking order is kept, but note that it never solved the overlap on its own
and cannot: `.gantt-bar`'s fill is translucent, so a bar behind another is still
visible through it.

### Draft scheduling and labels

File: `frontend/src/ScenePage.tsx`

- Pending `move_task` or `set_step_after` edits cause the display to use the
  last compiled step bars instead of `previewBars()`.
- Task view then applies `projectTaskMoveBars()` for pending task moves and
  `projectTaskAfterBars()` for pending dependencies, in that order.
- `draftProjection` (the "draft order · sync for timing" ruler and the disabled
  playhead seek) is on when either projection is active.
- `metaById` now stores labels generated with `prettyStepLabel()`, fixing the
  earlier raw long-label regression when preview is used elsewhere.

## Ghost overlap: measured root cause

### Root cause

A box-model asymmetry between the ghost and a real bar. Both use the
`.gantt-bar` class, but a real bar's inner element is a `<button>` while the
origin ghost's is a `<span>`, and the rule declared `inset: 0`, `width: 100%`,
`padding: 0 8px` and a 1px border with no global `box-sizing` reset. The UA
stylesheet gives form controls `border-box`, so a `<button>` sized exactly to
its frame; the `<span>` was on `content-box`, so its `width: 100%` gained the
16px of padding and 2px of border and the painted ghost hung **18px past its own
frame**, into the next task's bar.

This accounts for every earlier failed attempt: the 18px is independent of the
bar's duration (so clipping the ghost's duration changed nothing), independent
of its percentage width (so removing `min-width` changed nothing), and it is a
dashed border painted across the neighbour's translucent `rgba(55, 138, 221,
0.28)` fill (so `z-index` changed nothing).

Fix: `.gantt-bar` drops the over-constraining `width: 100%` — `inset: 0` already
sizes it to the frame — and pins `box-sizing: border-box` so no future element
swap can reintroduce the asymmetry.

### Measurement that settled it

The user's console dump for the failing lane, from
`ganttGhostDiagnostics.ts`:

| role | key | start | end |
| --- | --- | --- | --- |
| ghost | `move_apple_1_fridge:s0` | 36.5 | 86.166 |
| bar | `move_apple_1_fridge:open:s0` | 0 | 36.5 |
| bar | `move_apple_1_fridge:close:s0` | 86.166 | 114.266 |
| bar | `move_mug_1_sink:s0` | 114.266 | 131.066 |

Strictly adjacent, zero overlap in scheduled time — which eliminated the
aggregation hypothesis and pointed at rendering. The diagnostic was then
extended with `innerOverflowPx` / `innerBoxSizing`, because its first version
measured only the positioning frame and so could not have shown the 18px.

### Reproduction

1. Start with a compiled Task view where one robot has adjacent semantic task
   bars, for example `open fridge`, an apple-to-fridge task, and `close fridge`.
2. Drag the middle task onto the other robot lane so the operation is a
   cross-robot insert/reassignment.
3. Before Sync, inspect the origin lane.
4. The dashed origin ghost remains, but its tail visually enters the following
   real task. Depending on layout, borders and/or labels can look combined.

The user's screenshot showed the apple ghost extending into the start of
`close fridge`.

### Still-latent defect, not the cause here

`aggregateToTaskBars()` spans a task as `[min(member.start), max(member.end))`,
which is only sound while a task's compiled steps are contiguous on its robot. A
`#yield` step breaks that: it is inserted immediately before the yielding step,
carries `group = {stepId}#yield`, and has `repairKind === "yield"` — and only
`"detour"` is folded back via `parentGroup`. A yield landing inside a task's step
run makes that task's bar span across it, with or without any drag.

The measured reproduction contained no yield, so this did not cause the reported
overlap. It remains a real latent defect. If it ever shows up, split each
`robot + taskGroup` into contiguous runs sharing one `group` identity, and teach
`projectTaskMoveBars()` to move and ghost every segment of a group rather than
the first `findIndex` match.

### Things that were ruled out

- **Aggregation was not at fault in this reproduction.** See the measurement
  table above: the bars were strictly adjacent.

- **Deferred close binding does not interleave.** It looked like the obvious
  culprit (`plans[winner_robot][insertion:insertion] = inserted_steps` at the
  winner's live cursor), but the completion step it waits for is the reset
  *after* the placement, not the placement itself:

  ```python
  completion = placement
  if (placement_index + 1 < len(robot_steps)
          and robot_steps[placement_index + 1].get("op") == "reset"):
      completion = robot_steps[placement_index + 1]
  ```

  `select_deferred_close_owner` only reports a group ready once that completion
  is in `completed_end`, so the close group is always spliced in at a task
  boundary, after the placing task's trailing reset. A robot never starts the
  next task without resetting first, which is the correct behaviour.
- **`#reposition` legs** inherit the interrupted step's `group`, so they stay
  contiguous.
- **`#detour_` steps** are folded into their parent task via `parentGroup`.
- **`#go_to_rest`** requires the other robot's cursor to be exhausted, so it is
  appended, not inserted.

### Regression risk and how to re-measure

No test in the suite can guard this: jsdom performs no layout and the app's
stylesheet is not loaded in tests, so neither the DOM rectangles nor the computed
`box-sizing` exist there. The `.gantt-bar` rule carries a comment explaining why
`width` is absent and `box-sizing` is pinned; that is the only durable guard
available without real browser tooling.

The temporary `frontend/src/plan/ganttGhostDiagnostics.ts` used for the
measurement above was removed once the fix was confirmed (recover it from git
history if needed). It was a dev-only module called from a `GanttPanel` effect
whenever ghosts were present, reading `data-bar-key` / `data-ghost-key`
attributes on the bar frames, and it logged per lane:

- scheduled `start` / `end` / `duration` per bar, and whether any two bars on the
  lane overlap in scheduled time;
- each frame's `getBoundingClientRect()`, its width as a percentage of `total`,
  the computed `min-width`, and whether min-width was the binding constraint;
- `innerOverflowPx` and `innerBoxSizing` for the painted `.gantt-bar` inside each
  frame — the decisive columns. `innerOverflowPx` must be `0` on every row,
  ghost included, and `innerBoxSizing` must read `border-box` for both element
  types.

If a geometry problem is ever reported here again, rebuild that dump before
changing any code. Three fixes were attempted and reverted from schedule numbers
and stacking assumptions alone; the one measurement that included the painted
element's rectangle settled it immediately.

## Failed approaches already tried

### 1. Compacting the source lane

The first projection packed every affected source lane from its beginning.
After moving the apple task, the following orange task moved into the old apple
slot while the apple ghost stayed there. Both bars were rendered at the same
coordinates. This approach is incorrect and was removed.

### 2. Clipping ghost duration in the data model

The ghost duration was capped at the next compiled task's start and its CSS
minimum width was removed. The user reported no improvement. This change was
reverted, and correctly so: the schedule numbers were never wrong, and the 18px
overflow depended on neither duration nor percentage width.

### 3. Making the whole target task body an `after` snap zone

Endpoint-only snapping was replaced by snapping whenever the pointer was over a
target task body or within a margin. This consumed most/all of the other lane,
so unsnapped cross-robot insert stopped working. Both insert and `after` were
reported broken. The change and its tests were reverted.

The current design reuses the whole target body as the `after` hit area, but
only under Shift, so the two gestures no longer share a hit-test at all. Do not
reintroduce a geometric split between them.

### 4. CSS z-index only

The ghost was moved behind normal bars (`z-index: 0` versus `1`). The user
reported the display problem remained. Now explained: the fill is translucent,
so stacking cannot hide anything, and the real overflow was 18px of the ghost's
own box hanging past its frame.

### 5. Duplicate semantic groups hypothesis

Compiler-generated repair/detour bars can complicate semantic grouping, and
`aggregateToTaskBars()` groups by `robot + taskGroup`. A duplicate-group issue
was initially suspected, but later screenshots showed a deterministic source
compaction/overlap problem. Treat duplicate group identity as something to
inspect in real data, not as a confirmed root cause.

## Relevant files

- `frontend/src/plan/GanttPanel.tsx` — pointer hit-testing and drop dispatch
- `frontend/src/plan/ganttModel.ts` — task aggregation and task-move projection
- `frontend/src/ScenePage.tsx` — pending edit collection, frozen compiled bars,
  and edit callbacks including `handleSetTaskAfter`
- `frontend/src/plan/planEdits.ts` — `moveTask()`, `setStepAfter()`,
  `taskStepBounds()`
- `frontend/src/style.css` — Gantt bar, ghost, caret, and stacking styles
- `frontend/src/plan/GanttPanel.test.tsx` — pointer-gesture tests
- `frontend/src/plan/ganttModel.test.ts` — projection unit tests
- `frontend/src/plan/planEdits.test.ts` — edit-transform unit tests

## Verification status

At the current code state:

- `npx tsc -b --pretty false` passes.
- Full frontend suite passes: 34 files, 248 tests.

New pointer-gesture regressions in `GanttPanel.test.tsx`:

1. a purely vertical drag into the other lane reassigns (was a dead gesture);
2. a plain drag within the source lane reorders and never fires the dependency
   callback;
3. Shift-drag over the other lane's task body queues `onSetTaskAfter` in
   semantic ids, shows the `after` caret and no insert caret, and does not move
   the task — the target deliberately carries a generated `endStepKey` so the
   old silently-discarded path would fail this test;
4. releasing Shift mid-drag falls back to insert without further pointer
   movement;
5. a Shift-drag that stays on the source lane does nothing.

`planEdits.test.ts` covers `taskStepBounds()` and asserts that a generated tail
id makes `setStepAfter()` return the identical plan, which is the failure mode
the resolution fix exists to avoid.

`ganttModel.test.ts` covers `projectTaskAfterBars()`: edge alignment, lane
suffix cascade in program order, the no-earlier-than rule, and input immutability
with an unresolvable end.

Still missing, and unavoidable with the current tooling: jsdom performs no
layout, so no test in the suite can catch a rendered-geometry regression. The
ghost overlap needs the console diagnostic above or real browser tooling.
