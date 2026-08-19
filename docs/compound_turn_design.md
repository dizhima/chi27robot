# Compound Turn: Auto-Chained Author → Compile → Resolve

> Status: design approved 2026-08-06; ready to implement.
>
> Supersedes the "deferred phase: compound pipelines" section of
> `unified_conversation_author_resolver_design.md` — that deferral is now
> lifted, with the decisions recorded here. All other contracts from that
> document (event envelope, revision guard, terminal artifacts, context
> filtering, `RESOLVER_VERSION` rollback) remain in force.
>
> Motivation (short): the user-facing product has exactly **one plan
> artifact** — the latest resolved, conflict-free plan. Pre-resolve plans are
> pipeline internals and must never be a user-facing state. Additionally, the
> user study's **Baseline arm is literally this chain** (prompt → author →
> compile → resolve → replay with no structured editing), so the chain must
> exist regardless; Ours and Baseline then share one backend by construction.

## 1. Target architecture: two entrances, one tail

```text
NL prompt ────→ Author (NL → semantic actions) ──┐
                                                 ├─→ TAIL
manual edit (Gantt / scene) ─── no LLM ──────────┘
                          [edited items pinned]

TAIL:  Compile ──→ delegable conflicts? ── no ──→ commit artifact (fast path)
                          │ yes
                          ▼
                     Resolve (bounded, pinned-aware)
                          │
                          ▼
              commit: resolved plan + verified compile
```

Rules:

- **Author runs only for natural-language input.** Its sole job is
  translating NL into structured semantic actions. Manual edits are already
  structured semantic deltas — routing them through an LLM adds
  interpretation risk and latency for zero benefit.
- **Resolve is conditional**: skipped when Compile reports zero delegable
  conflicts. Most small edits take the fast path.
- Author / Compile / Resolve remain **separate modules** with their existing
  tool boundaries, tests, and rollback paths. The chain lives in the
  conversation/orchestration layer only.

### Routing table

| User input | LLM? | Path |
|---|---|---|
| NL prompt (semantic / preference / coordination) | Author | Author → tail |
| Drag task chip / change allocation / reorder (Gantt) | no | tail directly; edited items **pinned** |
| Change destination / waypoint (scene) | no | tail directly; edited items **pinned** |
| Read-only question | Explain (read-only) | no plan mutation |
| Merged sync button | no | manual tail trigger (retry / force re-sync) |

## 2. Prompt path: compound turn

One user turn = Author → Compile → (Resolve) → single terminal artifact.

- **Auto-commit.** The terminal artifact commits automatically; there is no
  "confirm draft → press Compile → press Resolve" ceremony. The safety net
  moves from pre-confirmation to **one-click revert** (plan history +
  existing `base_revision` guard against stale overwrites).
- **Progress events.** Reuse the phase-2/3 NDJSON stream: the compound turn
  emits the existing Author progress events, then compile/resolve stage
  events, into the same single live assistant message. Suggested stage
  values: `authoring`, `compiling`, `resolving`, `verified`.
- **Terminal artifact.** One `result` event carrying
  `{kind: "turn_result", plan, compile, report?, base_revision}` — the
  frontend adopts plan+compile directly (same rule as the existing resolver
  contract: never re-compile a returned V2 result).

## 3. Manual-edit path: batch, then apply

> **Revised 2026-08-06 after measuring the tail.** This section originally
> specified a ~800 ms debounced auto-tail, on the assumption that a tail run
> was cheap enough to fire per edit. It is not: a run is several full
> verification compiles (see the integration spec §10), so an auto-tail meant
> ten-plus seconds per drag, and edits made while thinking cancelled and
> restarted the run they were waiting for. An 800 ms window batches a shaky
> hand, not a considered set of changes.

- Edits accumulate as structured deltas. Each one updates the local preview
  immediately (instant feedback) and appends to a pending batch. **No tail
  runs.**
- The user applies the batch explicitly, with the **merged sync button** (§5).
  One press = one tail run over every pending edit, together.
- While the tail runs, show a lightweight "updating…" status on the plan
  artifact. On completion the plan updates in place.
- No Author call, no chat message required for this path; it may emit a
  compact activity line ("已根据你的修改重新验证计划") but must not create a
  fake conversational turn.
- The pending-edit count is itself the affordance: the user can see that
  changes are staged and that pressing sync is what commits them.

**What this gives up.** The original phrasing — "I dragged a block and the plan
made itself consistent" — was the perceivable form of propagation, and an
explicit press weakens it. The compensating claim is still strong and now
honest: the user never separately compiles and resolves, and never sees a
pre-resolve plan. Propagation is still automatic; its *timing* is now the
user's.

## 4. Pinned constraints (critical correctness rule)

Resolver's bounded repairs include reordering — without protection it can
**undo the user's manual edit in the same breath** ("I dragged it and it
snapped back"), which destroys steerability and violates the division of
labor (human decisions are residual intent; the agent may not override them).

- Every tail run receives a **protected set**: the allocations, orderings,
  destinations, and waypoints touched by the user's edits in this batch
  (prompt-path turns may also pin items the Author layer marks as explicit
  user decisions, e.g. "robot0 goes first").
- Resolve must treat protected items as hard constraints: repair only within
  the remaining degrees of freedom.
- If conflicts are unresolvable without touching a protected item, **defer
  honestly**: return the last verified plan + a warning naming the pinned
  item and the conflicting task ("你固定的顺序与 X 冲突"), handing the
  decision back to the user. Never silently unpin.
- Pin lifetime: pins apply to the tail runs triggered by that batch/turn.
  Whether pins persist across subsequent unrelated turns is out of scope for
  v1 (default: persist as plan attributes until the user changes that item
  again).

## 5. Merged sync button

- The separate Compile and Resolve buttons collapse into **one** button that
  triggers the tail manually.
- Since §3's revision it is the **primary** commit path for manual editing,
  not a fallback: apply a batch of edits, re-run after a deferral, force
  re-sync, or recover after an error — all the same press. It should be
  prominent when edits are pending and quiet when there are none.
- It reuses the exact same tail entry point (`intent_hint: "sync"` or
  equivalent) — no separate code path.

## 6. Failure semantics (progressive degradation)

| Failure | Behavior |
|---|---|
| Author fails | keep current plan; error in chat (existing behavior) |
| Compile fails | keep current plan; error names the failing task |
| Resolve fails / defers with no accepted candidate | commit the **compiled plan with remaining conflicts visible** + warning event; plan remains executable-invalid but inspectable; sync button re-runs |
| Resolve partial (accepted candidates then stop) | commit last verified partial result (existing V2 semantics) + warning |
| Stream disconnect / stale revision | never apply incomplete or stale artifacts (existing rules) |

A resolve failure must **not** fail the whole turn: the user still gets the
semantic change they asked for, plus an honest statement of what remains.

## 7. Study-facing configuration

- **Baseline arm** = this chain with the interaction surface disabled
  (no Gantt editing, no scene refs, no manual-edit path, no sync button;
  replay visible). One config flag on the frontend; backend identical.
- **Ours arm** = everything in this document.
- Log per tail run: trigger type (prompt / edit / button), stages run,
  conflicts found/resolved/deferred, wall + active time — this feeds the
  study's prompt-count and technical-eval metrics for free.

## 8. Acceptance criteria

1. A single NL prompt returns a resolved, conflict-free committed plan with
   no intermediate user action (when conflicts are resolvable).
2. A prompt whose compile yields zero delegable conflicts skips Resolve
   (fast path observable in logs).
3. A manual Gantt reorder runs no tail on its own; pressing sync runs exactly
   one, and the reordered items are never reverted by that run's resolve.
4. When a pinned item makes conflicts unresolvable, the turn ends with a
   deferral warning naming the pinned item; the user's edit is preserved.
5. Resolve failure after a successful author still commits the semantic
   change, with remaining conflicts visible.
6. Three consecutive edits followed by one sync press produce one tail run
   carrying all three, at any spacing — the batch is bounded by the press,
   not by a timer.
7. Revert restores the previous committed plan in one action.
8. Old `/author` and `/resolve_conflicts` endpoints still work
   (migration/rollback unchanged); `RESOLVER_VERSION=v1` still selectable.
9. With the baseline flag on, the frontend exposes prompt + replay only, and
   the same backend chain serves it.

## 9. Implementation touchpoints (verify against current code)

| Area | Likely location |
|---|---|
| Chain orchestration + routing | `src/mujoco_skills/orchestrator/conversation.py` / `service.py` |
| Conditional resolve + pinned set plumbing | resolver V2 entry (`resolver_v2.py`, `resolver_sessions.py`) — pass protected set into repair tool gating; no scheduler changes |
| Author event adapter reuse | `authoring.py` `on_event` |
| Debounced edit path + "updating…" state | frontend plan state (`frontend/src/plan/`, `ScenePage.tsx`) |
| Merged button | frontend; route through the same conversation entry with a sync hint |
| Plan history / revert | frontend plan store; keep last N committed artifacts |

Out of scope for v1: pin persistence policy across sessions, incremental
compilation, parallel mutating turns, any resolver session-budget changes.
