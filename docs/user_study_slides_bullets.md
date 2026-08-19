# Slide bullets — User Study Design (for internal report)

Paste-ready bullets; one `##` = one slide. Trim to taste.
Suggested talk order = slide order; if short on time, drop Slide 5, then 8.

---

## Slide 1 — Research Claim & What We Test

- **Claim:** vs. a fully autonomous NL pipeline (EMOS / LaMMA-P-style), a co-authoring workspace lets non-experts *steer* a multi-robot plan to match their intent
- Advantage should **grow** when intent is: incremental / deictic / **counter-default** (preferences no planner would pick on its own)
- **Not tested (by design):**
  - Planning optimality — same backend planner in both arms
  - `/resolve` quality — **deliberately automated, identical in both arms** (infrastructure; evaluated separately) — *the contribution is the division of labor, not the repair*
  - Scene-ref grounding — given to **both** arms (portable input feature)
  - Edit propagation vs. manual editing — manual arm cut; propagation = mechanism, shown in demo

---

## Slide 2 — The Manipulated Variable: One Artifact, Three Facets

- After equalizing everything else, the arms differ in **exactly one thing**:
  a **persistent, legible, locally-revisable plan artifact**

| Capability (Ours only) | Artifact facet | Measured by |
|---|---|---|
| Gantt / timeline | **legible** | verification: confirmed-but-wrong, checking behavior |
| Per-bar micro-edits | **locally revisable** | prompts vs. edits, revision cost |
| Multi-turn iteration | **persistent** | prompt-length growth, constraint survival, TLX |

- Facets are inseparable: Gantt w/o persistence = a screenshot; edits w/o Gantt = no handle; multi-turn w/o artifact = just chat → together = **the operational definition of co-authoring**
- Arms looking similar everywhere else is the design **working**: the remaining difference *is* the independent variable
- **Traceability (findings F1–3 → design goals DG1–3):** DG1 (valid team plan from intent — chat/refs/completion/repair) equalized across arms · **DG2 (inspectable) + DG3 (macro/micro manual steering) = the manipulated variable** · DG1's check-and-repair = infrastructure → technical eval

---

## Slide 3 — Two Conditions (within-subject)

| | **Baseline — Auto NL** | **Ours — Co-authoring** |
|---|---|---|
| Input | full-spec prompt + scene-ref clicks (object token as text) | chat + scene refs + Gantt edit + direct manipulation |
| Sees | execution replay only | task list, timeline, allocation, replay |
| Iterate | **stateless full re-specification** (edit previous spec text, full pipeline re-run) | local edits on a **persistent artifact** |

- **Fairness guardrails:** same LLM & pipeline (one backend, two frontends); unlimited attempts; replay in both; scene-ref in both
- **Not one-shot vs. multi-round** — both arms iterate freely; the contrast is *who maintains the evolving spec*: the user's text box vs. the system's artifact
- Baseline = literature-grounded: instantiates the autonomous quadrant (EMOS, RoCo, LaMMA-P — a planner consumes a spec, it does not own a conversation)

---

## Slide 4 — Task: One Scenario, Intent in Installments

- Post-party kitchen reset · 2 identical mobile manipulators · shared corridor, fridge/sink/cabinet
- **No written brief, no target image** — experimenter delivers each installment verbally, only after the previous one is confirmed
- Uniform ladder: **×1 = add tasks** (scope 2 → 4 → 5) · **×2 = inject a counter-default preference** (a choice the planner would never make itself → can only come from the human)

| # | Installment | Type |
|---|---|---|
| a1 | 2 cups → sink | scope (control, Baseline ≈ Ours) |
| a2 | "robot1 takes **both** cups" | counter-default **allocation** |
| b1 | + 2 apples → fridge | scope — does a2 survive? |
| b2 | "put **this** apple away first" (pointing; apples identical) | deictic **priority** — both arms can ref it; asymmetry = *verifying* it went first |
| c1 | + seasoning → cabinet | scope — a2 **and** b2 must survive |
| c2 | "robot1 stays out of the back corridor" | **route/space** preference |

- Cascade scope escalates: within one robot → across robots + door timing → whole-schedule routes
- Ladder ↔ facet coverage: a2 revise+legible · b1 persistent · b2 legible · c1 persistent · c2 legible+revise

---

## Slide 5 — What the Baseline Actually Looks Like (paper pilot)

- Mocked the full Baseline transcript: spec text **grows 10 → ~150 words** across installments (Ours: one sentence / one drag per installment, constant)
- Signature failure modes (predicted, pilot to confirm):
  - **constraint juggling** — one generation must satisfy 6 clauses at once; fixing c2 breaks b2
  - **verification tax** — identical apples: which went first? scrub the whole replay vs. read the timeline
  - **confirmed-but-wrong** — user believes it matched, hidden rubric says no
- Two coping strategies expected (coded per participant):
  - *declarative* — pile up "must / never / only" constraints (long, defensive)
  - *enumeration* — dictate each robot's sequence step by step = **user degenerates into the manual planner, LLM into a translator** — itself evidence that full-auto NL doesn't scale

---

## Slide 6 — Metrics

**Objective (auto-logged, every installment)**
- **Prompt count** to confirmation — *primary effort metric*, comparable across arms
- Outcome: **match / confirmed-but-wrong / timeout** (hidden rubric; prior constraints must survive) — any confirmed-but-wrong in Baseline = headline finding
- Prompt **length** curve (Baseline spec growth) · direct-manipulation breakdown (Ours, per channel) · sync presses logged separately
- Active time (secondary; wall time contaminated by pipeline latency)

**Subjective (per arm, 8 items)**
- UMUX-Lite (2) · Raw-TLX subset: mental / effort / frustration (3) · perceived control (3)
- Closing: preference ranking + interview (control vs. delegate; language vs. direct manipulation)

---

## Slide 7 — Logistics & Analysis

- Within-subject, **n = 16**, ~60 min/session (90 hard cap)
- Two **mirror-isomorphic** task variants V1/V2 (geometry mirrored, categories swapped) — no repeated content across arms, near-zero scene cost
- Counterbalance: arm order × variant assignment (4 groups × 4); mixed-effects model (arm fixed, participant random, variant/order covariates)
- Per-installment cap 5 min — pilot-calibrated against measured pipeline latency (~10 s+/run)
- Pre-study checks: Author accepts enumeration-style specs; c2 route constraint executable; baseline scene-ref composer

---

## Slide 8 — Companion Technical Evaluation (no participants)

- Scoped as **reliability of the loop**, not planner benchmarking
- **Author correctness** — scripted instructions → plan matches rubric
- **Propagation consistency** — scripted edits → downstream consistent, constraints survive
- **Conflict repair** — seeded conflicts (4 compile classes) → resolution rate, rounds, deferrals, collateral damage
- Cases = perturbations of study scenes; doubles as pre-study infrastructure validation
