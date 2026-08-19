# User Study Design — Co-Authoring Multi-Robot Tasks

> Status: design locked from discussion on 2026-08-05. Companion to
> `chi_positioning_draft.md` (positioning) and the formative study section
> (DC1–DC5). Pending: pilot calibration of time caps and variant equivalence.

## 1. What the study tests (and what it does not)

**Claim under test.** Compared with a fully autonomous NL pipeline
(EMOS / LaMMA-P-style), structured co-authoring lets non-experts steer a
multi-robot team plan to match their intent — and the advantage grows as
intent arrives incrementally, contains ambiguous references, and requires
verification.

**Explicitly not tested here:**

- *Planning optimality* — disclaimed in positioning; both arms share the same
  backend planner.
- *Edit propagation vs. manual editing* — the manual arm was cut (a manual
  Gantt editor is implementation-wise too close to our system, and single-robot
  EUP literature already covers that extreme). Propagation is described as a
  system mechanism and shown in the demo, not experimentally isolated.
- *`/resolve` repair quality* — resolve runs identically in both arms
  (controlled infrastructure). It is evaluated separately in the technical
  evaluation (§9).
- *Reference grounding (scene-ref)* — provided to **both** arms as an input
  feature (see guardrail 4); it is part of the system, not part of the
  paradigm contrast.

**The manipulated variable (what exactly is being tested).** After
equalizing everything else (same backend chain, same LLM, scene-ref in both
arms, replay in both arms), the arms differ in exactly one thing: a
**persistent, legible, locally-revisable plan artifact**. Ours' three extra
capabilities are the three faces of that single artifact, each instrumented
by specific measures:

| Capability | Artifact facet | Measured by |
|---|---|---|
| Gantt / timeline | **legible** (allocation, ordering, waits visible) | verification asymmetry: confirmed-but-wrong, checking behavior at b2/c2 |
| Per-bar micro-edits | **locally revisable** (change one thing without re-specifying) | prompts vs. edits per installment, revision cost |
| Multi-turn iteration | **persistent** (system maintains the evolving spec) | prompt-length growth, constraint survival, TLX mental load |

The facets are inseparable (a Gantt without persistence is a screenshot;
edits without a Gantt have no handle; multi-turn without an artifact is just
chat) — together they are the operational definition of co-authoring. That
the arms look similar everywhere else is the design working: the remaining
difference *is* the independent variable. Ladder coverage check: a2 →
revise+legible, b1 → persistent, b2 → legible, c1 → persistent, c2 →
legible+revise; all three facets loaded multiple times.

**Design rationale (one paragraph).** Any brief that can be read out in one
breath is a transcription task, and one-shot NL wins it — legitimately. The
study therefore never hands participants a complete written brief or a picture
of the target state. Intent is delivered in installments while a plan already
exists, includes **counter-default preferences** (allocation, priority,
routing) that no planner would choose on its own, and includes outcomes that
can only be verified against a legible plan (identical objects, ordering,
routes). These are the honest mechanisms (grounded in findings F1–F3) that
make the black-box paradigm pay its true cost: full re-specification,
constraint juggling, and unverifiable outcomes.

**Traceability (formative → system → study).** The formative study yields
findings F1–F3, mapped one-to-one onto design goals: **DG1** — turn user
intent into a valid team plan (chat, scene-ref, auto-completion, backend
check-and-repair) — is **equalized** across arms; **DG2** — make the plan
inspectable at task + execution level (timeline; in-scene routes, standoffs,
placements) — and **DG3** — manual steering at macro/micro levels (bar
edits, waypoint/placement edits) — together *are* the manipulated variable
(DG2 ↔ the legible facet, DG3 ↔ the revisable facet; persistence spans
both). Replay is shared by both arms and thus outside the manipulation.
DG1's validity checking/repair is controlled infrastructure, validated in
the technical evaluation (§9).

## 2. Conditions (2 arms, within-subject)

| | **Baseline — Auto NL** | **Ours — Co-authoring** |
|---|---|---|
| Input | Chat prompt + scene-reference clicks (click an object → its token is inserted into the spec text) | Chat + scene references + Gantt editing + direct manipulation (allocation, destination, waypoints) |
| Output visible | Execution replay (animation) only | Full artifacts: task list, Gantt/timeline, allocation, replay |
| Iteration | **Stateless full re-specification**: no conversation history; each attempt is one complete spec (the previous text stays editable in the box to remove the retyping confound), full author→compile→resolve re-run | Local edits and/or prompts on a **persistent artifact**; propagation keeps downstream consistent |
| Backend | **Identical** in both arms (same LLM, same author+resolve pipeline) | same |

**Baseline guardrails (it must be strong, not a strawman):**

1. Same backend, auto-chained — the baseline is our system with the
   interaction surface removed, not a weaker system.
2. Unlimited attempts; full re-specification *is* the autonomous paradigm's
   iteration (a planner consumes a spec, it does not own a conversation —
   EMOS/LaMMA-P are exactly this). The contrast is therefore **not**
   one-shot vs multi-round: both arms iterate freely; what differs is *who
   maintains the evolving spec* — the user's text box or the system's
   structured artifact.
3. The baseline always sees the execution replay; it lacks only the
   structured, editable artifacts.
4. **Reference grounding is given to both arms.** Scene-ref is a portable
   input feature, orthogonal to the paradigm contrast; withholding it would
   let differences at deictic installments be attributed to a cheap missing
   feature instead of the paradigm. In the baseline it acts as a text macro
   (unambiguous object token in the spec); in Ours the same click binds to
   the live plan artifact (object ↔ task highlighting).

The baseline directly instantiates the autonomous quadrant of the positioning map
(EMOS, RoCo, LaMMA-P), which makes the baseline literature-grounded.

## 3. Task design — one continuous scenario, 6 installments

Post-party kitchen reset, two homogeneous mobile manipulators, shared back
corridor, named facilities (fridge, sink, cabinet). One evolving plan; the
experimenter delivers each installment **verbally / on a card, only after the
previous installment is confirmed** — there is never a full readable brief.

The ladder follows one uniform structure: every **×1 adds tasks** to the
existing plan (scope growth), and every **×2 injects a counter-default
preference** — a decision the system's own planner would *never* choose, so
it can only come from the human. Counter-defaultness is the immunity
condition: no ×2 can be attacked with "shouldn't the optimizer do that?"
(the earlier "nearer cup" / "lighter workload" drafts failed exactly this
test). Deixis is a *modality*, not an installment type — it appears where
object reference is naturally ambiguous (b2).

| # | Installment (delivered verbally) | Type | Design rationale |
|---|---|---|---|
| a1 | "Put the two cups in the sink." | Scope (2 tasks) | Honest control point; Baseline ≈ Ours expected |
| a2 | "Have robot1 take care of both cups." | **Counter-default allocation preference** | Default is one cup each; the planner would never pick this. Smallest cascade: reassignment + reordering within one robot |
| b1 | "Also put the two apples in the fridge." | Scope (+2 tasks) | **Inheritance check**: does a2's allocation survive the re-plan? |
| b2 | "Put *this* apple away first, before everything else." (experimenter points; the two apples are visually identical) | **Deictic priority preference** | Priority has no scene-derivable basis. Both arms can reference the apple via scene-ref click; the asymmetry is **verification**: did it actually go first? Baseline scrubs the replay of two identical apples; Ours reads the timeline / clicks the apple to highlight its task. Cascade crosses robots: ordering + fridge-door timing |
| c1 | "Also put the seasoning in the cabinet." | Scope (+1 task) | Second inheritance check (a2 **and** b2 must both survive) |
| c2 | "Robot1 should stay out of the back corridor — route it around the island." | **Route / space preference** | User's reason lives outside the scene ("I'm working there"). Largest cascade: paths, waits, and corridor contention across the whole schedule |

Complexity rises on two axes at once: task count (2 → 4 → 5) and cascade
scope of each preference (within one robot → across robots + door timing →
across the whole schedule's routes). The three preference channels
(allocation / priority / route) also map one-to-one onto Ours' interaction
surfaces (allocation drag / scene-ref + one sentence / waypoint or one
sentence), while the Baseline must re-verbalize the entire accumulated
preference stack at every installment — the incremental-installation vs.
start-from-scratch contrast is the mental-load mechanism under test.

*Implementation check for c2:* route preferences must be executable
(waypoint constraint or corridor avoidance). If Author/compile cannot honor
"avoid the corridor," downgrade c2 to a same-flavor spatial preference
(e.g., "robot1 finishes everything on its own side before crossing").

**Per-installment cap: 5 minutes** (pilot-calibrated). At the cap the
installment is scored as a timeout and the experimenter restores/continues.

## 4. Scenes and matched variants

Two variant task sets **V1 / V2**, used so no participant repeats content
across arms:

- **Mirror isomorphism** (cheap + maximally equivalent): same room geometry
  mirrored; object categories swapped (cups↔bowls, apples↔oranges);
  fridge/cabinet sides exchanged. Fixture topology unchanged → skills
  retarget, no new scene authoring.
- Structure held identical: same installment types in the same order, same
  object counts, same corridor/facility coupling.
- Plus one training scene (S0) for interface familiarization per arm.

Identical-task repetition is rejected deliberately: carryover is asymmetric
(participants would anticipate installments and pre-state constraints in B,
destroying the installment mechanism).

## 5. Counterbalancing and analysis

- Fully crossed: **arm order (2) × variant assignment (2)** → 4 groups;
  target **n = 16** (4 per cell). Variant difficulty differences wash out
  because each variant appears in each arm equally often.
- Analysis: mixed-effects model — arm as fixed effect, participant as random
  effect, variant and order as covariates.
- Preference-channel contrasts (allocation vs. priority vs. route) are
  **exploratory**, not primary.
- Pilot check: within-arm completion time for V1 vs. V2 within ~20%.

## 6. Measures

### Objective (logged automatically, every installment)

| Measure | Definition | Role |
|---|---|---|
| **Prompt count** | # of LLM round-trips until the participant confirms the installment | **Primary effort metric**, comparable across arms |
| **Outcome adjudication** | match / **confirmed-but-wrong** / timeout, scored against a hidden rubric (each installment = 1 checkpoint + survival of all prior constraints; regression folds in here) | Outcome safety net. Any confirmed-but-wrong in the baseline is a headline finding (unverifiable black box) |
| Direct-manipulation breakdown (Ours only) | # drags / allocation changes / scene-ref clicks / waypoint edits, per channel | Descriptive; feeds the modality-fit / division-of-labor analysis. Not summed with prompts (qualitatively different costs) |
| User-active time & wall time | active = composing/acting, excluding system latency | Secondary / descriptive only (wall time is contaminated by compile/resolve latency, which differs systematically between arms) |

Viewing the Gantt/replay is perception, not an operation — never counted.

### Subjective (once per arm, standard instruments)

| Measure | Instrument |
|---|---|
| Ease of use | UMUX-Lite (2 items; validated short form, maps onto SUS scores — do not hand-pick SUS items, which voids the benchmark) |
| Workload | Raw TLX subset (3 items): mental demand, effort, frustration (physical demand irrelevant; performance covered by objective outcome) |
| Perceived control / steerability | 3 custom 7-point items (e.g., "I could make the plan become what I wanted") |

### Closing (after both arms)

Preference ranking + why; ~5–8 min semi-structured interview
(what they controlled vs. delegated; when they chose language vs. direct
manipulation).

## 7. Procedure and time budget (~60–70 min)

| Block | Time |
|---|---|
| Intro + consent | 5 |
| Arm 1: training (3–4) + 6 installments (~12–15) + questionnaires (4) | ~22 |
| Arm 2: same | ~22 |
| Ranking + interview | 8–10 |
| **Total** | **~60 (90 hard max)** |

## 8. Experimenter discipline

- Never show a target image/video of the final state; never hand over a
  readable full brief.
- Deliver each installment only after the previous one is confirmed; use
  identical scripted wording; for deictic installments, point at the screen.
- Scoring rubric stays hidden from participants.
- Scripted restore points between installments in case of system failure.
- **Confirmation requires a synced plan** (manual edits commit via the sync
  button; local previews don't count): an installment is confirmed only when
  no edits are pending and the participant states they are done. Training
  must cover the sync button explicitly. Sync presses are logged as their own
  event type (neither a prompt nor an edit; excluded from primary metrics).

## 9. Companion technical evaluation (separate from the user study)

Backend reliability is evaluated without participants, scoped as
**reliability of the loop, not planner optimality** (avoid inviting
LaMMA-P/MAPF benchmark comparisons):

| Component | Input | Judged on | Metrics |
|---|---|---|---|
| Author correctness | Scripted NL instruction sets (incl. installments, references) | Semantic plan matches instruction rubric | Task-level accuracy |
| Propagation consistency | Scripted semantic edits injected into existing plans | Downstream (allocation/timing/door steps) auto-consistent; prior constraints survive | Consistency rate, constraint-regression rate |
| Resolve repair | (scene, plan) instances seeded with the four compile conflict classes: shared-facility contention, corridor/space overlap, door-state coupling, temporal deadlock | Conflict resolved, plan still valid | Resolution rate per class, rounds, compile count, wall time, deferral rate, collateral-damage rate |

Cases are generated by perturbing the study scenes (object counts, placements,
task sets) — no new scenes. Report deferral/failure taxonomy honestly
(resolver has session budgets; 100% is neither expected nor claimed). This
doubles as pre-study infrastructure validation.

## 10. Open items

- [ ] Pilot: calibrate 5-min cap **against measured tail latency (a
      compile+resolve run takes ~10s+; Baseline re-prompts run the full
      chain)**; verify V1/V2 equivalence.
- [ ] Verify c2 feasibility: can Author/compile honor a route/corridor
      constraint? If not, apply the documented downgrade.
- [ ] Implement scene-ref in the baseline composer (click → object token
      inserted as text; no artifact binding).
- [ ] Verify Author accepts imperative per-robot scripts (enumeration-style
      specs), not only declarative constraints — a baseline strategy that
      must not fail for system reasons.
- [ ] Write the hidden scoring rubric (checkpoint list per installment).
- [ ] Build V2 mirror variant + retarget skills.
- [ ] Instrument logging: prompt count, per-channel edit events, active time,
      confirmations.
- [ ] Draft the 3 custom control/steerability items.
- [ ] Update `chi_positioning_draft.md`: coordination-grounded reframe,
      LaMMA-P row in §3.2/§3.3 (decided, not yet applied).
