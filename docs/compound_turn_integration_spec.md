# Compound Turn — Integration Spec

> Status: draft for review, 2026-08-06.
>
> Implements `compound_turn_design.md` against the code as it exists today.
> The design doc says *what* the chain must do; this doc says *which functions
> change, in what order, and which of its open questions had to be decided to
> make it fit*. Contracts from `phase0_unified_conversation_contracts.md`
> (event envelope, context filtering, `RESOLVER_VERSION` rollback) remain in
> force.

## 1. What exists today

| Piece | Location | Compound-turn relevance |
|---|---|---|
| Turn router (author \| resolve \| explain, mutually exclusive) | [`dispatch_conversation_turn`](../src/mujoco_skills/orchestrator/service.py:336) | must gain a chained path |
| Author stage, emits its own `result` | [`stream_author_turn`](../src/mujoco_skills/orchestrator/conversation.py:130) | must become a stage, not a turn |
| Resolve stage, emits its own `result` | [`stream_resolve_turn`](../src/mujoco_skills/orchestrator/conversation.py:378) | same |
| Reusable resolve pipeline | [`resolve_v2_core`](../src/mujoco_skills/orchestrator/service.py:230) | reused as-is, plus one new arg |
| Backend compile | [`_compile_via_skill_service`](../src/mujoco_skills/orchestrator/service.py:126) | hardcodes `retain_snapshot: False` |
| Public compile shape | [`_public_compile_result`](../src/mujoco_skills/orchestrator/service.py:216) | has no `compile_id` |
| Delegable-conflict predicate (backend) | [`delegable_conflicts`](../src/mujoco_skills/orchestrator/conflict_payload.py:184) | the correct gate for "skip resolve" |
| Delegable-conflict predicate (frontend, duplicate) | [`delegableConflictCount`](../frontend/src/ScenePage.tsx:227) | drifts from the above; retire |
| **Author-path compile runs in the browser** | [`usePlanCompile`](../frontend/src/plan/usePlanCompile.ts:100) | must move server-side for the tail |
| Adopt a verified compile without re-POSTing | [`adoptCompileResult`](../frontend/src/plan/usePlanCompile.ts:67) | reused unchanged |
| Derived resolve overlay | [`ScenePage.tsx:98`](../frontend/src/ScenePage.tsx:98)–115 | re-framed (§2, D1) |
| Terminal artifact application | [`applyOutcome`](../frontend/src/ScenePage.tsx:442) | gains a `turn_result` branch |
| Manual edits (plan-level mutations) | [`planEdits.ts`](../frontend/src/plan/planEdits.ts) | no delta record exists (§2, D2) |
| Ownership pin already in the resolver | `robot_locked` — [`conflict_payload.py:147`](../src/mujoco_skills/orchestrator/conflict_payload.py:147), [`resolver_v2.py:113`](../src/mujoco_skills/orchestrator/resolver_v2.py:113) | the channel pins ride on |
| `base_revision` | **does not exist anywhere** | new |

## 2. Decisions

> **Terminology.** The plan as `decompose` emits it — before the resolver adds
> anything — is the **authored plan** (`AuthoredPlan`, the type the frontend
> already uses). Do not call it "baseline": `compound_turn_design.md` §7 uses
> **Baseline arm** for the study's control condition, and one word for two
> unrelated things in the same doc set will eventually cost someone an hour.

### D1 — Keep two plan layers; flip which one is user-facing

The overlay ([`resolvedPlan`](../frontend/src/ScenePage.tsx:98)) exists because
resolve injects artifacts (`robot0#go_to_rest` steps, cross-robot `after`
edges) that must not contaminate the authored plan. The design doc's "one plan
artifact" does not remove that hazard — it only removes the *ceremony*.

**Decision.** Both layers stay; their roles swap.

| Layer | Today | After |
|---|---|---|
| authored plan (was `committedPlan`) | what the user sees before pressing Compile; directly editable | pure recomputation input: `decompose(semanticActions)` + replayed edit deltas; **never rendered** |
| `livePlan` (was `resolvedPlan`) | temporary overlay, discarded on any edit | the **only** user-visible plan; produced by every tail run |

Because every tail run **recomputes** the authored plan rather than appending
to its own previous output, resolver artifacts cannot accumulate across
consecutive manual edits. [`planMerge.mergeAppendedTasks`](../frontend/src/plan/planMerge.ts)
and the pure-append promotion in [`applyOutcome`](../frontend/src/ScenePage.tsx:455)
are therefore **deleted**: they exist to preserve an earlier resolution across
an author turn, and a full re-resolve every turn supersedes that need.

**Corollary — the frontend never holds the authored plan.** `decompose` is
backend-only, so the frontend could not build it anyway; it does not need to,
because the tail runs server-side and regenerates it from `current_actions`
(which the frontend already sends). The authored plan exists only *inside* one
backend turn. Frontend state therefore reduces to: `semanticActions` + `edits`
(inputs), `livePlan` + `liveCompile` (display), `history` (revert). No second
plan object, which is what retires the `draftPlan`/`committedPlan` pair.
`draftPlan` survives only as an optimistic local preview for manual edits
(§5.17), never as a committable state.

*Rejected — delete the overlay, run the tail incrementally on one committed
plan.* Smallest diff, but each manual edit would re-resolve a plan that already
contains the previous run's `go_to_rest` legs and `after` edges, so artifacts
snowball; it would need a strip-artifacts pass that is strictly harder to get
right than keeping the authored layer clean.

### D1b — Pure-append promotion is restored (corrects D1)

**Observed after S2 shipped.** Turn 1 ("move the mugs to the sink") resolved a
sink conflict with 2 repairs. Turn 2 ("also move the apples to the fridge")
re-derived the plan from scratch and spent 2 of its 5 repairs re-solving that
same sink conflict — then ran out of budget with 5 conflicts unfixed.

D1 deleted `planMerge.mergeAppendedTasks` on the grounds that resolver
artifacts would snowball. **That reasoning was wrong for this path.**
`mergeAppendedTasks` was conditional: it grafted only when the turn was a pure
append, and fell back to a clean recompute otherwise. The snowball hazard is
real for *incremental manual edits* (D1's rejected alternative), not for a
guarded pure-append promotion. Deleting it made every turn re-pay for all
prior coordination — and because `MAX_SESSIONS_PER_RUN = 3` bounds a run to
three conflict groups, that re-payment *evicts* the new turn's real work. The
cost grows with plan length, so it degrades exactly where the study needs it
to hold up.

**Decision.** Restore the promotion, on the **backend** (the tail now assembles
the plan), and test it at the **action** level rather than by diffing step
trees — `decompose` sets `task = action.id`, so action identity is the
authoritative key and is far more robust than the old plan diff.

- Pure append iff the new action list starts with the previous one unchanged
  (same ids, **every field equal**, same order) and adds only at the end.
  `current_actions` already carries the previous turn's actions; the frontend
  additionally sends `previous_plan` (its `livePlan`).
  Compare **all** `AugmentedAction` fields, not a hand-picked subset:
  `_steps` also reads `facility`, `via_points`, `place_at_pin`, `target`,
  `serves`, so a subset check lets "route robot0 around the left side, and
  also move the bowl" pass on its unchanged-*looking* prefix and then reuse
  the previous steps — silently dropping the reroute. Over-comparing only
  falls back to a clean recompute (correct, slower); under-comparing is
  silently wrong.
- When it holds: compile/resolve from
  `{tasks: previous_plan.tasks + decompose(new_actions).tasks[len(prev):]}`.
  Take the appended tasks from a decompose of the **full** new list, not of the
  extras alone — `_steps` carries per-robot state (`previous_action_ended_reset`)
  across actions, so decomposing the tail in isolation can differ.
- Otherwise: clean recompute, exactly as now.
- **Strip trailing `#go_to_rest` tasks for any robot receiving appended work.**
  A departure anchor is only meaningful as a robot's last act; grafting after
  it yields work → park → work → park across turns. Let the resolver re-insert
  it if still needed (`_departure_prerequisite_candidates` already skips
  duplicates via `if departure_id in by_id`).

### D2 — Manual edits are recorded as deltas on the authored plan

[`planEdits.ts`](../frontend/src/plan/planEdits.ts) exposes plan-level
mutations (`setTaskRobot`, `setStepAfter`, `setStepAt`, `setStepViaPoints`, …).
Only robot reassignment flows back to the semantic layer, via
[`applyRobotOverrides`](../frontend/src/plan/authorPlan.ts). Under D1 the
authored plan is regenerated from `semanticActions` every turn, so any edit not
captured at the action level would be silently erased by the next turn.

**Decision (revised — see D2b).** ScenePage holds an ordered
`edits: PlanEditDelta[]`. The same list is the source of the tail's
**protected set** (§3) — one structure, two uses.

### D2b — Edits apply to the resolved plan, not to a re-derived authored plan

**Observed after S3 shipped.** On a resolved plan the user edited the waypoints
of a `go_to_rest` step. Two symptoms, one cause: the edit had *no effect at
all*, and the turn re-resolved a conflict that had already been fixed.

D2 originally said the backend replays deltas onto
`decompose(semanticActions)`. **That contradicts D1.** D1 makes the *resolved*
plan the only plan the user ever sees — so it is also the only plan they can
edit — and a resolved plan contains steps the authored layer cannot express:
`go_to_rest` legs and cross-robot `after` edges the resolver created. Rebuild
from `decompose(actions)` and those steps do not exist, so a delta targeting
one has no target (dropped), and every resolver repair is discarded at the same
time (conflicts return, and get "re-fixed"). Editing an artifact was not an
edge case to handle later; it is the ordinary act of adjusting the plan in
front of you.

**Decision.** The tail's input is chosen per path:

| Path | Input |
|---|---|
| edit tail | previous **resolved** plan + this batch's deltas |
| sync, no pending edits | previous resolved plan, unchanged |
| author turn, pure append (D1b) | graft onto previous resolved plan — manual edits survive for free |
| author turn, not a pure append | clean recompute; manual edits are lost **and said so out loud** |

Consequences worth stating:

- **The persistent replay ledger goes away.** It existed only because every
  turn rebuilt from scratch. Once a delta is applied it is baked into the plan,
  so `edits` means "not yet applied" and is cleared on commit. This also
  removes the `editLedgerRef` / `pendingEditCount` split.
- Losing manual edits on a non-pure-append author turn is a real cost, accepted
  because the user restructured the tasks those edits were tweaking. It must be
  reported, never silent.
- Pins stay batch-scoped, which is what design §4 mandates as the minimum;
  their *effect* persists because the edit is now part of the plan.
- D1's snowball worry (rejected alternative) does not bite here: these deltas
  MODIFY existing steps rather than adding artifacts, and D1b's
  strip-trailing-departures plus the resolver's own `if departure_id in by_id`
  guard keep repairs idempotent.

**Risk to close during implementation:** replaying a delta requires the target
id to survive regeneration. Deltas are therefore keyed by
`{action_id, group, op}` where derivable, and fall back to a raw step id only
for step-local edits. A delta whose target no longer exists after an author
turn is **dropped with a visible activity line** (`"你对 X 的手动调整已被这次改动取代"`),
never silently.

### D2c — A structural edit withdraws the resolver's departure anchors

**Observed after D2b shipped.** On a resolved plan the user reassigned two
tasks between robots. The obsolete `go to rest` leg — parked there for the
*old* allocation — stayed. Before D2b it had disappeared, but only as a side
effect of the rebuild-from-scratch that D2b removed: the same erasure that
made the leg vanish was the erasure of every repair, which was the D2b bug.
Both behaviours were two faces of one defect.

The real asymmetry: **the resolver only ever adds repairs; nothing withdraws
them.** A departure anchor is not a fact about the plan, it is a *conclusion*
— "this robot parks here so that one can enter" — derived from who does what,
in what order. Change those premises and the conclusion is stale, but it
persists as historical inertia: a pointless parking leg plus a cross-robot
`after` edge that over-constrains the schedule.

**Decision.** When a manual-edit batch contains a delta that **actually
landed** and is *structural* (`set_task_robot`, `set_step_after` —
`plan_edits.STRUCTURAL_OPS`, mirroring the frontend's `draftEditImpact`
bucket), strip every robot's trailing departure anchor before compiling
(`decompose.strip_stale_departures`). Resolve then re-derives one if the new
structure still warrants it — `apply_insert_go_to` is idempotent and
`_departure_prerequisite_candidates` skips a robot that already has one.

- **Spatial edits do not strip.** A waypoint or standoff moves a path, not a
  coordination decision; withdrawing the anchor would only cost an attempt to
  re-derive the same thing.
- **Broader than D1b's `gaining_robots` filter, on purpose.** An anchor is a
  relationship between a *parker* and a *waiter*; a reassignment can
  invalidate it through either end, so "robots this edit touched"
  under-covers. Over-stripping costs at most one resolve attempt;
  under-stripping silently ships a worse plan.
- **Keyed on the applied subset.** A dropped delta changed no premise, so it
  justifies no withdrawal. Compared by identity, not equality — two deltas on
  the same step can be legitimately equal by value.
- **Removal must purge the references.** `apply_insert_go_to` writes a
  navigate *and* its reset, and the anchor exists precisely because another
  robot's step carries `after: [<robot>#go_to_rest]`. That waiter is usually
  not the robot being stripped, so popping the task alone leaves a dangling
  edge and `compile_plan` refuses the plan outright (`step 'X' depends on
  unknown step id(s)`, `_container_dependency_order`). The purge is part of
  the removal in `_strip_trailing_departures`, covered by a test asserting no
  surviving `after` names a removed step.

**Not done here:** resolver-added cross-robot `after` edges that are *not*
attached to a departure anchor go stale by the same argument, but withdrawing
them needs provenance the plan does not carry (nothing distinguishes a
resolver edge from a user-pinned one). Keeping a stale edge over-constrains
the plan; dropping a needed one breaks it — so the safe side is to keep, until
edges carry an origin tag.

### D3 — `resolve` stops being a natural-language intent

With the tail auto-running, "把冲突修一下" is satisfied by any author turn, and
the explicit re-run is the merged sync button (`intent_hint: "sync"`).

- A second classifier variant, `COMPOUND_INTENTS = ("author", "explain")` +
  `COMPOUND_CLASSIFY_PROMPT`, drops the ~30 lines of author-vs-resolve
  disambiguation, removing a whole misclassification class.
  **The legacy `INTENTS` / `CLASSIFY_PROMPT` are kept verbatim and stay the
  default**; the shrunk variant is selected only by the `COMPOUND_TURN`
  branch (`classify_intent(..., compound=True)`). A global shrink would change
  flag-*off* behavior — free text like "resolve the conflicts" would stop
  reaching the resolver — which is exactly what the rollback path must not do.
- Under the flag, an `intent == "resolve"` can only come from the button, so
  dispatch rewrites it to `intent_hint: "sync"` + `plan` from
  `plan_state.completed_plan` before calling the tail: no natural language to
  translate, therefore no author stage. The eligibility gate still applies.
- [`_resolve_gate_fails`](../src/mujoco_skills/orchestrator/service.py:327) +
  `RESOLVE_PRECONDITION_MESSAGE` survive **only** as the sync button's
  no-plan-yet guard.
- `POST /resolve_conflicts` and `POST /author` are untouched (design §8.8).

### D3b — Resolve is multi-run by design; the tail must not assume one run converges

`run_resolution_v2` is **not** a one-shot. Its budget
([`resolver_state.py:12`](../src/mujoco_skills/orchestrator/resolver_state.py:12))
is `SESSION_ATTEMPT_CAP = 6`, `MAX_SESSIONS_PER_RUN = 3` — a run admits at most
**three conflict groups**; the rest are parked in `state.deferred_conflicts`
and reported via `session_deferred`. The existing user-facing text for that
event says so out loud: *"kept for the next Resolve"*
([`conversation.py:267`](../src/mujoco_skills/orchestrator/conversation.py:267)).
Removing the Resolve button removes the affordance that sentence points at.

`ResolutionState` is constructed fresh per run, so a second run re-admits the
remaining groups — "press Resolve twice" already works today and is exactly
what the tail must reproduce.

**Decision.** Bounded auto-continue plus the sync button as backstop.

- After a resolve pass, run again until the normalized verified state repeats.
  The state signature covers the committed plan, unresolved conflict
  fingerprints, reason distribution, and processed/deferred session IDs.
  Conflict count alone is not a progress signal: a valid repair may replace a
  conflict one-for-one or advance to a different conflict session.
- Three independent stops, because study scenes routinely exceed 5 conflict
  groups and a blind pass count is the wrong knob:
  1. **progress guard** (above) — an identical normalized state stops with
     `no_progress`; a first non-terminal zero-repair result gets one retry so
     model/session deferrals can change before it is declared stalled.
  2. **pass cap** `COMPOUND_RESOLVE_MAX_PASSES`, default **4** (covers up to 12
     conflict groups at 3 groups/pass). Env-tunable so a scene can be retuned
     without a code change.
  3. **wall-clock deadline** `COMPOUND_TAIL_DEADLINE_SECONDS`, default **240**.
     Verification compiles, not LLM calls, dominate tail latency, and their
     cost scales with plan length rather than pass count — so a deadline is the
     only bound that actually tracks the expensive resource. Checked *between*
     passes only; a pass in flight always finishes so the returned plan is
     always a verified one.
- Stop immediately when every remaining `unresolved[].reason` is
  `no_remaining_legal_strategy` rather than a cap reason
  (`session_attempt_cap_reached` / `global_attempt_cap_reached` / session-cap
  deferral): those are genuinely unfixable and another pass only burns tokens.
- Whatever remains is surfaced honestly and points at the **sync button**,
  whose first documented purpose is "re-run after a deferral" (design §5).
  `resolver_progress_text`'s `session_deferred` string is retargeted
  accordingly.
- `MAX_SESSIONS_PER_RUN` is **not** raised (design §10 puts resolver
  session-budget changes out of scope).

`turn_result.report` includes `passes`, `pass_cap`, and `stop_reason` so study
logs can distinguish convergence, repeated state, deadline, and budget stops.
For a non-converged turn, those fields plus the unresolved-reason histogram
are rendered under **See details**; the existing user-facing warning remains
in the main bubble.

### D5 — The terminal message is composed, not regenerated

Today the two stages end differently on purpose: author's `message` is written
by the LLM, while [`resolve_summary_text`](../src/mujoco_skills/orchestrator/conversation.py:325)
is a deterministic template *"so the accountability text cannot drift from what
actually happened"* (its own docstring). A compound turn emits one
`message_completed`; handing that whole string to the LLM would forfeit the
guarantee.

**Decision.** Concatenate, never regenerate:

```
<author.message>                       ← LLM, semantic change only

<resolve_summary_text(report, plan)>   ← template, verbatim
```

- Fast path (no delegable conflicts): author message + one templated line
  ("No conflicts — the plan is ready."). `resolve_summary_text`'s existing
  converged-with-no-actions branch ([:348](../src/mujoco_skills/orchestrator/conversation.py:348))
  covers this.
- Edit-triggered tail (no author stage): resolve summary only, and it goes to
  the activity line, not a chat message (design §3).
- Deferral: the template's non-converged clause ([:366](../src/mujoco_skills/orchestrator/conversation.py:366))
  is extended to name the sync button.

### D4 — "Skip resolve" uses the resolver's own predicate

The fast path is gated on
[`delegable_conflicts(compile_result)`](../src/mujoco_skills/orchestrator/conflict_payload.py:184)
— the exact function that builds the resolver queue — so "we skipped resolve"
can never disagree with "the resolver had nothing to do". The frontend's
duplicate `kind === "path" || "facility"` count
([ScenePage.tsx:227](../frontend/src/ScenePage.tsx:227)) becomes display-only
and is no longer a gate.

## 3. New data structures

```ts
// frontend/src/plan/planEdits.ts
export type PlanEditDelta =
  | { op: "set_task_robot"; target: EditTarget; robot: RobotName }
  | { op: "set_step_after";  target: EditTarget; after: string[] }
  | { op: "set_step_at";     target: EditTarget; at: [number, number, number] }
  | { op: "set_step_via";    target: EditTarget; via: number[][] };

export type EditTarget = { actionId?: string; group?: string; stepId?: string };
```

```jsonc
// protected set — wire form, backend `body.protected`
{
  "allocations": [{"group": "task3", "robot": "robot0"}],
  "orderings":   [{"before": "task1", "after": "task3"}],
  "destinations":[{"step": "robot0#place2"}],
  "waypoints":   [{"step": "robot1#move4"}]
}
```

```jsonc
// terminal artifact
{
  "kind": "turn_result",
  "turn_id": "...",
  "plan": { "tasks": [...] },        // nested, resolved
  "compile": { "schedule": [], "warnings": [], "conflicts": [],
               "completed": {}, "compile_id": "..." },
  "report": { ... },                  // present only when resolve ran
  "actions": [...],                   // present only when the author stage ran
  "base_revision": 12,
  "stages": ["authoring", "compiling", "resolving", "verified"],
  "deferred_pins": [{"pin": "...", "conflicting_task": "..."}]
}
```

`base_revision` is a monotone integer owned by the frontend, bumped on every
commit and every local edit, sent in the turn body and echoed in the artifact.
An artifact whose `base_revision` ≠ the current one is **discarded**, not
applied.

## 4. Backend changes

**`conversation.py`**

1. Extract `_run_author_stage(body, write_event, *, provider, manifest) -> AuthorStage | None`
   from [`stream_author_turn`](../src/mujoco_skills/orchestrator/conversation.py:130):
   everything up to and including the `decomposition` progress event, returning
   `{actions, plan, message, reason}` and emitting **no** `result` /
   `message_completed`. `stream_author_turn` keeps its current signature and
   behavior by wrapping it (rollback path intact).
2. New `stream_compound_turn(body, write_event, *, provider, manifest, compile_fn, snapshot_lookup_fn)`:
   author stage (skipped when `intent_hint in ("sync", "edit")`) → compile →
   conditional resolve, auto-continued per D3b → one `turn_result`.
3. The author stage's terminal progress text becomes a
   `decomposition_text` parameter of `_run_author_stage`. The compound caller
   passes `"Plan drafted; compiling."`; the legacy wrapper keeps
   `"Draft ready for your review and compile."` as the default. The line
   promises what happens next, and what happens next differs by path — a
   global replacement would make the legacy flow's own progress text a lie.
4. `resolve_summary_text` ([:325](../src/mujoco_skills/orchestrator/conversation.py:325))
   gains a pin-deferral clause, and its non-converged clause names the sync
   button instead of "the next Resolve" (D3b/D5). Same for
   `resolver_progress_text`'s `session_deferred` string
   ([:267](../src/mujoco_skills/orchestrator/conversation.py:267)).
5. `AUTHOR_PROGRESS` and `resolver_progress_text` are otherwise **reused
   verbatim** — both already emit `progress` into the shared envelope, so the
   frontend's existing `activities` accumulator
   ([`conversationTypes.ts:29`](../frontend/src/conversation/conversationTypes.ts:29))
   collects the whole chain into one message with no frontend change. Only two
   wrapper stages are new: `compiling` and `verified`.
6. `INTENTS` / `CLASSIFY_PROMPT` shrink per D3.

**`service.py`**

7. `_compile_via_skill_service(plan, *, retain_snapshot=False)`. The tail's
   compile passes `True` — it is the user-visible compile, and its `compile_id`
   is what a later sync press reuses.
8. `_public_compile_result` returns `compile_id`.
9. `resolve_v2_core(..., initial_compile_result=None)` — the tail already holds
   the round-0 compile; passing it through avoids a redundant compile and keeps
   `report["compile_count"]` honest (mirroring the existing
   `initial_snapshot_reused` bookkeeping at [:267](../src/mujoco_skills/orchestrator/service.py:267)).
   The same argument carries pass 2's input (D3b) without a re-compile.
10. `dispatch_conversation_turn`: `explain` → unchanged; everything else →
    `stream_compound_turn`, behind `COMPOUND_TURN=1` (default off in slice S1).

**Pins — `conflict_payload.py` / `resolver_v2.py`**

11. The protected set is threaded into focus-payload shaping, the same layer
    where `robot_locked` already filters candidates.
12. **Non-negotiable rule, learned from the `robot_locked` deadlock** (see the
    comment at [`resolver_v2.py:117`](../src/mujoco_skills/orchestrator/resolver_v2.py:117)):
    a pin may only veto a candidate that would *actually change the pinned
    attribute*. Per tool:

    | Tool | Changes | Vetoed by |
    |---|---|---|
    | `insert_go_to` | appends a retire-leg to the same robot | **nothing** (reassigns no task, reorders no task) |
    | `add_after` | adds a dependency edge | an ordering pin the edge would invert |
    | `replan_path` | rewrites a mover's via-points | a waypoint pin on that step |
    | `handoff_terminal_close` | reassigns a close | an allocation pin on that task (already `robot_locked`) |

13. A focus left with `allowed_tools == []` *solely* because of pins emits
    `pin_deferred` (`{pin, conflicting_task}`) and lands in
    `report["deferred_pins"]`. The last verified plan is kept; nothing is
    silently unpinned.

## 5. Frontend changes

14. `TurnOutcome` gains `turn_result`; `conversationStreamClient` gains its
    artifact branch and sends `base_revision` + `protected`.
15. ScenePage: rename per D1, drop `planMerge`, delete the "stage as draft, wait
    for Compile" gate ([:445](../frontend/src/ScenePage.tsx:445)), apply the
    tail's plan+compile through the existing `loadScheduleTracks` +
    overlay-state path ([:486](../frontend/src/ScenePage.tsx:486)).
16. `edits: PlanEditDelta[]` + `replayEdits`; each `planEdits.*` call site also
    pushes a delta.
17. **Manual-edit tail — batch, then apply (revised; design §3 revised too).**
    Shipped in S3 as an 800 ms debounced auto-tail, and changed after using
    it. A tail run is several full verification compiles, so auto-running per
    edit meant ten-plus seconds per drag, and an edit made while thinking
    aborted the very run it was waiting for — that abort is also what
    surfaced the dead-socket crash in `_open_ndjson_stream`. An 800 ms window
    batches a shaky hand, not a considered set of changes.

    - An edit updates the local preview and appends to a pending batch. It
      starts **no** tail.
    - The **merged sync button (item 19)** applies the whole batch in one run.
      Same tail entry point; still no synthetic chat turn; still an
      "updating…" badge plus a compact activity line.
    - Keep the one-in-flight `AbortController` (a second press while a run is
      in flight supersedes it) — but the debounce timer goes away.
    - The button shows **no count**. A number implies the user should track it,
      and there is nothing useful to do with "3" that "you have unapplied
      changes" does not already convey. Enabled/prominent is the whole signal.
18. **Plan history — one-click revert.** This is not a new feature; it is the
    replacement for the Compile-press review gate that item 15 deletes
    (design §2: the safety net moves from pre-confirmation to one-click
    revert). Without it there is no way back from a turn the user did not
    want — including resolver changes they never asked for.

    - **Depth `N = 1`** for v1: one step back. See §11 for why this is a
      deliberately reversible choice.
    - A node is a **full state snapshot**, not just what is displayed:
      `{revision, plan, compile, semanticActions, edits}`. Restoring only
      `plan`/`compile` produces split-brain — the Gantt shows the restored
      plan while the next author turn still runs on the newer
      `semanticActions`, silently undoing the undo with no visible cause.
      `items` are deliberately **not** stored (each carries a real
      `SkillTrack`); restore re-runs `loadScheduleTracks(compile.schedule)`,
      which loads rather than compiles.
    - **Every committed artifact gets a node, converged or not.** A plan
      committed with unresolved conflicts (§7) is exactly the state a user is
      most likely to want out of.
    - **Node boundary ≠ tail-run boundary.** A tail run is "when the system
      recomputes"; a node is "when the user considers one change done". One
      chat turn is always one node. Consecutive *edit-triggered* tail runs
      coalesce into a single node for the whole editing episode — the
      compound-turn equivalent of "everything done before pressing Compile".
      **An editing episode ends at the next chat turn or sync press —
      deliberately not on an idle timer.** A timer creates invisible state:
      the user cannot tell whether their next drag joins the previous node or
      starts a new one, and undo has to be predictable to be trusted.
      Item 17's revision makes this nearly trivial: with edits applied only by
      an explicit press, one press *is* one commit *is* one node, and the
      batch it carries is exactly the episode. The coalescing machinery stays
      (a second press while a run is in flight still supersedes it) but no
      longer has to reconcile several auto-fired commits per episode.
    - Explain turns create no node.
19. **Merged sync button — pulled forward from S4 into item 17's revision.**
    Replaces the Compile and Resolve buttons. No longer visually secondary:
    since item 17 stopped auto-running, this button IS the manual-edit commit
    path. Prominent when edits are staged, quiet otherwise — **no count**.
    It remains the recovery path for a D3b deferral, so it must
    stay reachable whenever `report.converged === false` even with no pending
    edits (that press is a plain re-resolve).
20. `usePlanCompile`'s auto-compile-on-change is now only reached by the sample
    plan seed. Left in place for v1; retiring it (route the seed through the
    tail with `intent_hint: "sync"`) is a follow-up.
21. The `activities` accumulator needs **no** change (item 5), but the process
    view must stay readable at compound length: a two-pass resolve can emit
    ~30 progress lines. Collapse by default, show the last line live.

## 6. Event contract

One turn, one live assistant message:

```
message_started
intent_selected  {intent: "author"}        "Understanding your plan change."
progress {stage: "augment"}                "Filling in shared-facility open/close/dependency steps."
progress {stage: "propose_plan"}           "Plan structure fixed; generating execution steps."
progress {stage: "decomposition"}          "Plan drafted; compiling."          ← text changed (item 3)
progress {stage: "compiling"}              "Compiling the plan."               ← NEW
progress {stage: "run_started"}            "Checking conflicts in the compiled plan."
progress {stage: "focus_started"}          "Working on the overlap near sink between robot0 and robot1."
progress {stage: "strategy_selected"}      "Sending robot0 to its parking spot to clear the sink."
progress {stage: "verification_started"}   "Change generated; running a full compile to verify."
progress {stage: "round_completed"}        "That adjustment worked; checking remaining conflicts."
progress {stage: "run_completed"}          "Final verification done; producing the summary."
progress {stage: "resolving_again"}        "More conflicts than one pass covers; continuing."  ← NEW (D3b)
   … second pass repeats run_started … run_completed …
warning  {…}                               ← session/pin deferral, partial resolve
progress {stage: "verified"}               "Verified."                         ← NEW
result   {artifact: turn_result}
message_completed                          author.message + "\n\n" + resolve_summary_text  (D5)
```

Every line above except the four marked ones is existing text, emitted by
existing code, unchanged. Stage names for the wrappers match design §2.

Edit-triggered tails run the same sequence minus `intent_selected` and the
author stages, and their `message_completed` is routed to an activity line
rather than a chat message (design §3).

## 7. Failure semantics → code

| Failure | Where | Behavior |
|---|---|---|
| Author raises | `_run_author_stage` | one `error` event, no `result`; plan untouched |
| Compile raises | `_compile_via_skill_service` | `error` naming the failing task; plan untouched |
| Resolve raises / defers with nothing accepted | `resolve_v2_core` | commit the **compiled** plan + `warning`; conflicts stay visible; `report.converged=false` |
| Resolve partial | existing V2 semantics | commit last verified partial + `warning` |
| Pin makes it unresolvable | §4.11 | last verified plan + `pin_deferred` naming pin and conflicting task |
| Stale `base_revision` | frontend | artifact discarded |

A resolve failure must never fail the turn.

## 8. Tests (mapped to design §8)

| # | Criterion | Test |
|---|---|---|
| 1 | prompt → resolved committed plan, no user action | backend: fake provider + fake `compile_fn`, assert one `turn_result` with `report.converged` |
| 2 | zero delegable conflicts skips resolve | assert `resolve_v2_core` never called; `stages` lacks `"resolving"` |
| 3 | Gantt reorder never reverted | resolver unit test: ordering pin + `add_after` that would invert it → candidate absent |
| 4 | pinned unresolvable → honest deferral | assert `pin_deferred` + last-verified plan returned |
| 5 | resolve failure still commits the semantic change | `resolve_v2_core` raises → `turn_result` still carries author's plan + compile |
| 6 | 3 rapid edits → 1 tail run | frontend: fake timers, assert one fetch |
| 7 | revert | frontend: history stack restores previous artifact |
| 8 | old endpoints intact | existing `/author` + `/resolve_conflicts` tests must pass unchanged |
| 9 | baseline arm | frontend flag hides Gantt editing / refs / sync; same backend path |

Additional tests for D3b / D5:

| Criterion | Test |
|---|---|
| >3 conflict groups → 2 passes, no user action | fake compile with 5 facility conflicts; assert `report.passes == 2` and the second pass admits the deferred groups |
| repeated state stops without looping | two passes return the same plan + unresolved/reason/session signature → `stop_reason == "no_progress"` |
| equal count can still progress | pass 1 keeps the same conflict count but changes the verified plan/fingerprint → pass 2 runs |
| no auto-continue when genuinely stuck | all `unresolved[].reason == "no_remaining_legal_strategy"` → one pass only |
| pass cap holds | 20 conflict groups → `passes == COMPOUND_RESOLVE_MAX_PASSES`, remainder deferred with a sync-button pointer |
| deadline holds | fake clock past `COMPOUND_TAIL_DEADLINE_SECONDS` after pass 1 → no pass 2, plan still verified, `report.stop_reason == "deadline"` |
| terminal message is composed | assert `message_completed` starts with the author message and contains the verbatim `resolve_summary_text` output |
| fast path message | zero delegable conflicts → author message + the converged-no-actions line, no resolver text |

Additional tests for D2c:

| Criterion | Test |
|---|---|
| structural edit withdraws the anchor | reassign a task on a plan with `robot0#go_to_rest` → no departure task in the artifact, and the earlier repair marker still survives |
| spatial edit does not | `set_step_standoff` on the same plan → anchor still present |
| a dropped structural delta withdraws nothing | `set_task_robot` on a ghost action → reported dropped, anchor untouched |
| removal leaves no dangling edge | unit: after stripping, every surviving `after` names a step that still exists (the waiter robot is not the stripped one) |
| stripping is pure + idempotent | input plan unmutated; `strip(strip(p)) == strip(p)` |

Also non-regression: `insert_go_to` is still offered when an allocation pin is
present (the deadlock repro from the `robot_locked` fix, re-run with pins).

## 9. Implementation slices

- **S1 — backend tail. DONE.** Items 1–10 behind `COMPOUND_TURN=1`, default
  off; tests 1, 2, 5, 8 + all of D3b/D5 + dispatch routing. Full suite 181
  passed. Every flag-off deviation found in review was reverted to a
  flag-gated variant (D3's classifier, item 3's progress text), so "existing
  tests pass unmodified" holds literally.
- **S2 — frontend adopts `turn_result`. DONE.** Items 14, 15, 18, 21. Single
  visible plan + revert; tests 1, 7.

  **Rollback changes shape here.** Once the frontend converges (item 15), it
  can no longer serve `author_result` — that artifact *requires* the draft +
  Compile gate this slice deletes. Carrying both state models in one
  `ScenePage` is worse than either. So: the frontend requires the backend to
  run `COMPOUND_TURN=1`, and the frontend's rollback is reverting the S2
  commit. The backend env flag keeps protecting the backend independently
  (S1's tests still pin the legacy path). The stream client must fail a
  legacy artifact with an actionable message — "backend is not running
  COMPOUND_TURN=1" — not the generic unknown-kind throw.
- **S3 — manual edits. DONE**, then twice corrected by use:
  items 11–13, 16, 17; tests 3, 4, 6 + the deadlock non-regression.
  - Item 17's debounced auto-tail was replaced by an explicit sync press
    (item 17 revised) once it became clear a tail run costs several full
    compiles. That pulled **item 19 forward out of S4**.
  - D2's replay model was replaced by D2b after editing a `go_to_rest` step
    proved a no-op that also erased prior repairs.
- **S4 — surface. Mostly already absorbed.**
  - Item 19 (merged sync button) — **DONE** in item 17's revision.
  - D3's classifier shrink — **DONE** in S1, as a flag-gated variant
    (`COMPOUND_INTENTS` / `COMPOUND_CLASSIFY_PROMPT`, selected only under
    `COMPOUND_TURN=1`) so the rollback path keeps the three-intent classifier.
  - Item 20 (`usePlanCompile` retirement) — explicitly deferred by §12.
  - **Remaining: the baseline-arm flag (test 9), and design §7's per-run
    logging.** Both are study-facing rather than product-facing, and the
    logging is also the prerequisite §10 names for arguing about latency from
    numbers instead of impressions.

Each slice is independently shippable and revertible via the env flag.

## 10. Parked: turn latency and the waiting experience

Observed in manual testing: a two-task turn is tolerable, a six-task turn is
not. Where the time actually goes, worst first:

1. **Verification compiles.** Every executed resolver candidate triggers a
   FULL compile (`verification_started` — "running a full compile to verify").
   A turn accepting 5 adjustments pays ≥5 compiles plus the rejected attempts,
   the initial one, and the final. D3b's second pass doubles the ceiling.
   Compile cost scales with plan length, so this grows superlinearly with the
   session — exactly the direction a study session moves.
2. **Author LLM rounds** — several tool calls per turn, roughly constant.
3. D1b's promotion attacks (1) by not re-resolving old conflicts, which is why
   it matters more than it first appeared.

Three directions, not mutually exclusive:

- **Progressive commit.** Show the compiled plan the moment compile lands,
  before resolve runs, then update in place. The user sees their tasks appear
  immediately and resolve becomes visible refinement rather than dead air. In
  slight tension with "one plan artifact", but arguably more honest than a
  spinner — and it needs a rule for what the Gantt shows meanwhile
  (conflicts visible? greyed?).
- **A real waiting affordance.** Item 21 collapses the process trail by
  default, which is backwards for a 60-second turn. Elapsed time, the current
  line shown prominently, and a sense of position ("adjustment 3") would each
  help more than they cost.
- **Compile reuse.** The snapshot registry already exists (`compile_id`,
  `_snapshot_via_skill_service`), but resolver candidate compiles deliberately
  opt out (`retain_snapshot: False`). Whether a verification compile can reuse
  anything is an open question worth measuring before assuming it can't.

Measure before optimising: instrument per-stage wall time in the tail (design
§7 already wants per-run logging) so this is argued from numbers.

### D7 — Per-step compile memoization by compile-order prefix

Acting on (1). `sg.compile_plan` is already a per-step loop whose only state
carriers are explicit: `contexts[robot]` and `shared_world`. A step's output is
therefore a pure function of (its params, the state reaching it), and that
state is a deterministic function of the **compile-order prefix**. So memoize
on that prefix.

```
key[i] = H(compiler_generation(), canonical(ordered_steps[0..i]), canonical(next_step[i]))
```

Cache the step's `items` **and the post-state** (that robot's context +
`shared_world`), because `compile_robot` mutates both; a hit must fast-forward
state, not just return items. A run hits for a contiguous prefix and computes
normally from the first divergence.

**Why prefix-keying rather than "only `after` changed, so geometry is
unchanged".** That shortcut was the first idea and it is unsound: `after` feeds
`deps` in `_container_dependency_order` (skill_generators.py:4067), so an added
edge can relinearize the topological order and change what later steps see.
Prefix-keying gets the same win *derived* instead of assumed — when an added
edge does not perturb the order, every prefix hash is unchanged, every step
hits, and only `schedule()` + `detect_conflicts()` re-run. When it does perturb
the order, hits stop exactly at the divergence, with no special case to get
wrong.

**Expected payoff.** Most resolver repairs change little or no geometry — in
one observed turn, 5 of 6 adjustments were `add_after`. Verification compiles
dominate tail latency (above), and they are the ones that should now be nearly
free.

**This is a correctness-critical cache.** A wrong hit yields a
plausible-but-wrong trajectory, the one failure mode this system can least
afford. Non-negotiables:

- Anything that cannot be canonicalised confidently (unknown op, unhashable
  field) is a **miss**, never a guess.
- A verification mode (env-gated) recomputes on every hit and compares, so the
  cache can be validated against the full suite before it is trusted.
- Add `TRACKS` to `compiler_generation()`'s material: base track files feed
  compiled output but are not currently part of the generation identity.
- Bounded LRU; log hit/miss per step alongside the existing
  `compile_time_sec` instrumentation so the hit rate is observable.

**Shipped. Measured, not estimated** (8-step two-robot plan, real compiles):
repeat compile 3.51s → 0.06s (**62x**); a repeat carrying a non-relinearizing
`after` edge — the resolver's verification-compile case — also 0.06s. Full
results compare IDENTICAL between memo on and off, including the edged plan
against a from-scratch compile. Memory is a non-issue: ~0.05 MB per entry,
~30 MB at the 512-entry cap.

### D7b — key on reaching state, and double-index by `completed_step`

D7 was correct but hit only 8–25% in production. Measured cause, two
independent problems:

| scenario | before | after |
|---|---|---|
| authored plan, cold | 0/8 | 0/8 |
| authored again | 8/8 | 8/8 |
| **`completed` plan (what the resolver actually sends)** | **0/8** | **8/8** |
| `completed` again | 8/8 | 8/8 |

- **Cause A.** Round 0 compiles the *authored* plan; every verification compile
  sends a plan derived from `completed`, which carries compiler-filled fields
  (`standoff`, `robot`, `_author_order`, `_robot_order`). Identical geometry,
  different key — so the first and most expensive verification compile missed
  every step. Fix: index each entry **also** under the key computed from its
  own `completed_step`. That form is the compiler's own normalized output, so
  keying by it is sound, and it sidesteps having to adjudicate field by field
  which of those are geometry-relevant — `standoff` in particular MUST stay in
  the key, since a user can edit it.
- **Cause B.** Keying on the global compile-order prefix was too coarse:
  appending one step to one robot changed how the two robots interleave in
  `_container_dependency_order`'s linearization, so the *other* robot's steps
  missed too, despite byte-identical reaching state. Fix: key on the reaching
  state itself —
  `H(generation, digest(context), digest(shared_world), canonical(step), canonical(next_step))`
  — which is the true input set, and is invariant to interleaving that does not
  change state.

**A latent, pre-existing bug surfaced by `verify`.** `completed_step["at"]`
and `["standoff"]` were rounded to 3 decimals. That is fine for display, but
`completed` is *resubmitted as real geometry*, so every resolver round-trip was
quantising placements to the millimetre — enough to nudge a `place` step's
redundant wrist DOF onto a different (still valid) IK solution. This predates
D7 entirely; the memo just made it observable. Rounding at the round-trip-
critical sites is now 6 decimals. `verify`'s comparison gained a 1e-5
tolerance to clear the residual microradian noise — note this widens only the
"is it the same answer" bar *after* the key matched; digests and
canonicalisation upstream stay byte-exact, and 1e-5 is still 400x tighter than
the IK's own `tol=4e-3`.

**Independently re-measured after D7b:** repeat compile 6.23s → 0.08s (74x);
full results still compare IDENTICAL between memo on and off. Suite passes in
all three modes (227). `verify` now takes 6m21s versus 84s for `on` — that gap
is itself the evidence that it is recomputing on real hits, which it barely
did before D7b raised the hit rate.

**`verify` mode has a blind spot — do not treat it as total coverage.** Its
hit branch is gated on `mode == "on"`, so under `verify` a cached entry is
never actually installed as live state. It proves "the stored value was right
when stored", NOT "using it corrupts the store". The latter is real: a hit
installs the cached `context`/`shared_world` as live state and the NEXT
`compile_robot` mutates them in place, which would write later state back into
an earlier entry and silently mis-fast-forward every future hit. What prevents
that is the deep copy in `_step_memo_get`/`_step_memo_put` — those copies are
load-bearing, not defensive slack, and `verify` passing is not evidence they
can be removed.

## 10b. D8 — What belongs in the bubble vs. behind "See details"

Observed in use: the chat bubble carries the whole templated resolve summary
("Resolved all conflicts with 5 adjustments: 1…5, total plan time 114s →
137s"), while "See details" carries the per-attempt progress trail ("Trying to
adjust execution order…", "That adjustment did not help; trying another
option." ×6, "Change generated; running a full compile to verify."). That is
backwards, and D7/D7b made it worse by making attempts cheap enough to run
more of them.

**The distinction is live vs. retained, not important vs. unimportant.** The
attempt trail earns its place *while the turn runs* — for 30 seconds it is the
only evidence the system is working (§10). Once the turn ends it is mechanism
noise. The adjustment list is the opposite: useless live (it does not exist
yet), and it is the accountability artifact afterwards — the whole reason D5
keeps it templated rather than LLM-written.

**Decision.**

| | during the turn | after |
|---|---|---|
| bubble | latest progress line + think-dot (unchanged) | author message, plus the unresolved-conflicts warning if any |
| See details | — | the numbered adjustments + total compound-turn wall-clock time |
| attempt trail | shown live, one line at a time | not retained |

- The **non-converged warning stays in the bubble**, never behind the toggle.
  "9 conflicts could not be fixed automatically" is exactly the sentence a user
  must not have to go looking for.
- Split at the source: the backend sends the author message and the resolve
  summary as separate fields on `turn_result`, rather than the frontend
  splitting a composed string. This revises D5's composition rule — the
  guarantee D5 exists for (the accountability text is templated, never
  regenerated by the LLM) is unaffected by *where* it renders.

## 11. Parked: history depth as a product feature

`N = 1` is set for v1 as an undo affordance, not because one step is the right
long-term answer. A deeper history is a different feature: a set of plan
*alternatives* the user can replay and compare ("show me the version before I
pinned robot0 to the sink"), which is squarely in the coordination-transparency
story the study is about.

Two notes so v1 does not foreclose it:

- The node shape above is already a **full snapshot**, so raising `N` is a
  storage change, not a redesign. Do not "optimise" it into a diff stack.
- Alternatives want *meaningful checkpoints*, whereas undo wants *coarse
  nodes*. Those pull the episode-boundary rule (item 18) in different
  directions — settle the product question before raising `N`, not after.

## 12. Out of scope (v1)

Pin persistence across sessions, incremental compilation, parallel mutating
turns, resolver session-budget changes, retiring `usePlanCompile`.
