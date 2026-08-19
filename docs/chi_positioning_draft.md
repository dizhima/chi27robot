# CHI Positioning Draft — Agentic Workspace for Multi-Robot Task Authoring

> Draft artifacts from the direction-setting discussion:
> **(1) Thesis + Contribution**, **(2) North-star vignette**, and
> **(3) Related-work positioning** (three axes).
> These are starting points to edit, not final copy.

---

## 1. Thesis + Contribution

### 1.1 One-sentence thesis

> We present an **agentic co-authoring workspace** that lets **non-experts** direct a team of
> (heterogeneous) robots in a MuJoCo simulation through **language and sketch**: the human
> supplies task *intent*, an LLM agent fills in the executable details, and the human refines
> the result through **simulation feedback**.

### 1.2 Expanded framing (the problem)

Multi-robot tasks such as *tidy-up*, *sort-and-transport*, or *arrange* are not fully specified
by the environment. Their target state depends on decisions that only exist in the user's
head — what belongs where, how things should be arranged, what to avoid, what matters most.
Prior autonomous multi-robot systems (EMOS, RoCo) assume the goal is **given** and focus on
robot–robot coordination; conventional simulation authoring tools (CoppeliaSim, Isaac) assume
an **expert** who edits the scene by hand. Neither supports a **non-expert** who wants to *author*
what a robot team should do and *steer* it when the result is wrong.

### 1.3 Guiding principle (residual intent)

> Optimization does what it is good at — finding optimal solutions to a **given** objective.
> The human supplies only the part of the objective/constraints that **cannot be recovered from
> the environment alone** (preferences, semantics, ambiguity, trade-offs, tie-breaks).

Operational test — the **residual-intent test**: *Given the scene and a reasonable default
objective, is the desired outcome uniquely determined?* If yes → automate it (don't put it on
the human). If no → it is an authoring decision worth exposing.

### 1.4 Contribution statement (draft, 3 points)

1. **Team-intent authoring — an interaction paradigm for multi-robot task authoring by
   non-experts.** It decomposes an under-specified task (e.g., tidy-up) into a set of *authoring
   decisions* over a **team whose coordination structure is open** (who / when / with whom / which
   team / under what preferences), and lets the human resolve any decision via the
   **cheapest-fitting modality** — language for abstract/semantic/multi-object/conditional intent,
   sketch and direct manipulation for spatial intent — while the agent proposes defaults, reasons
   over the team, and executes. *(Contrast with single-agent goal authoring, where the residual is
   a determinate, automatable "how" — see §3.5.)*
2. **A system realizing the full co-authoring loop** in a browser-based React + MuJoCo workspace:
   scene editing → language goal → capability-aware allocation → multi-robot execution →
   **inspectable/editable artifacts** (plan, subtasks, schedule, waypoints, metrics) →
   simulation-grounded **feedback and repair** ("this collided / didn't grasp / reroute").
3. **An empirical study with non-experts** showing they can author multi-robot tasks they could
   not otherwise specify, and characterizing **the division of labor** between human and agent
   (which decisions humans want to control, which they delegate) and **modality fit** (when
   language beats direct manipulation and vice versa).

### 1.5 What makes it *not* thin (guarding against "RoCo + a UI")

The contribution is **not** any single feature (each — scene edit, allocation, language-to-sim —
has precedent). It is the **intersection** of four properties, which is empty in prior work:
full-loop coverage × multimodal division of labor × non-expert target user × simulation-feedback
author-correct iteration. The demo must make authoring **load-bearing and visible**: a
no-intervention baseline produces an ambiguous/sub-optimal/failing result, and a small human edit
**visibly** improves a measurable outcome (makespan, collisions, success rate, goal match).

### 1.6 Authoring decisions in tidy-up (the design surface)

Tidy-up = resolving an under-specified target state. Each decision can be an **agent default** or
**human-authored**; co-authoring is the two jointly resolving them. These nine decisions are
referred to as **DoF1–DoF9** throughout this document.

| # | Decision | Authored by human when… | Fitting modality | Status |
| --- | --- | --- | --- | --- |
| DoF1 | Scope (what to tidy) | items to ignore aren't obvious | selection + language | supporting |
| DoF2 | Grouping | grouping is by owner/intent, not a visible attribute | selection + language | supporting |
| **DoF3** | **Destination mapping** | mapping is preference, not a fixed rule | point / drag + language | **pillar** |
| **DoF4** | **Spatial arrangement** | the target layout exists only in the user's head | **sketch** + language | **pillar** |
| **DoF5** | **Symbolic constraints** | forbidden zones, grouping, capacity, fragile→container/arm | language | **pillar** |
| DoF6 | Priority / partial goals | order/importance is a human call | language / timeline | supporting |
| DoF7 | Robot allocation | capability/reachability trade-offs, human override | drag subtask chip | supporting |
| DoF8 | Scheduling | parallel/sequential, handoff, shared-space tie-break | language / timeline | supporting |
| **DoF9** | **Routing** | avoid semantic zones, prefer a route, break deadlocks | **sketch (soft constraint)** | **pillar (reframed)** |

**Three pillars for the first paper:** spatial arrangement (DoF4), preference destination +
symbolic constraints (DoF3+DoF5), routing-as-soft-constraint (DoF9).
**Explicitly automated (not given to the human):** optimal collision-free paths, deterministic
attribute sorts, reachability checks. **Dropped:** physical-quality constraints (force/"gentle").

---

## 2. North-Star Vignette (RoboCasa kitchen island tidy-up)

The first showcase is deliberately small. Its purpose is not to demonstrate a new allocation or
motion-planning algorithm. It makes the **human–agent co-authoring loop** visible: a user gives an
under-specified goal, the agent proposes an inspectable multi-robot task, and the user corrects the
part of the intent that the scene alone cannot reveal.

### 2.1 Setting

The scene is a fixed RoboCasa kitchen with a central island, a sink, and a refrigerator. The team
contains two **homogeneous mobile manipulators**, `Robot A` and `Robot B`, positioned on opposite
sides of the island. Five existing RoboCasa objects are mixed across the island rather than
pre-grouped by category:

- three visually distinguishable cups;
- two pieces of fruit.

The intended default destinations are simple: cups go into the sink, while fruit goes into the
refrigerator. Putting the fruit away is the showcase's one compound fixture interaction:
**open refrigerator → place both fruits → close refrigerator**.

The kitchen layout and object set are fixed in this version. The user's scene interaction is
**selection for reference grounding**, not full scene editing: a selected rendered object maps to
its corresponding XML object, so language such as “this one” is executable and unambiguous.

### 2.2 Co-authored flow

1. **Under-specified request.** The user says, “Clean up the kitchen island.”
2. **Agent interprets the goal.** From the visible object categories and available skills, the
   agent proposes the target mapping: all three cups → sink; both fruits → refrigerator.
3. **Agent exposes alternatives for team organization.** Rather than silently committing to one
   allocation, it presents two understandable strategies:
   - **Plan A — destination-based (recommended):** `Robot A` handles all cups and `Robot B`
     handles both fruits plus the refrigerator interaction. The agent recommends it because the
     roles are clear and the compound interaction stays with one robot.
   - **Plan B — region-based:** each robot clears the side of the island closest to it, so both
     may handle a mixture of cups and fruit.
4. **User chooses a team strategy.** The user accepts Plan A, while retaining the ability to
   choose Plan B or manually change an individual task's robot assignment.
5. **Inspectable shared artifact.** The workspace shows a hierarchical, editable task list:
   - `Robot A`: cup 1 → sink; cup 2 → sink; cup 3 → sink.
   - `Robot B`: open refrigerator → place fruit 1 and fruit 2 → close refrigerator.
   Selecting a scene object highlights its task-list entry and selecting an entry highlights the
   corresponding scene object.
6. **Human supplies residual intent.** One cup is still in use, but that fact is not recoverable
   from geometry or object category. The user selects cup 3 and says, “This one is still in use;
   don't put it away.”
7. **Agent revises the shared plan.** The cup 3 task remains visible but is marked **keep in
   place**, preserving the edit history instead of silently disappearing. Under Plan A, the final
   work is now balanced: `Robot A` puts away two cups and `Robot B` puts away two fruits.
8. **Confirm and execute.** The user approves the revised task list. The two robots execute the
   selected plan, including the refrigerator compound action, while cup 3 remains untouched.

The interaction model is an open-ended **propose → inspect → revise → confirm** loop. The showcase
uses one revision for brevity, but the user can continue changing scope, destinations, strategy,
or assignments until satisfied.

### 2.3 What the showcase demonstrates

- **Co-authoring, not language remote control.** The agent contributes semantic defaults,
  executable decomposition, and multiple team strategies; the user contributes intent that the
  environment cannot determine.
- **Scene-grounded multimodal reference.** Language expresses the correction while selection
  identifies the exact XML-backed object.
- **Multi-robot task authoring without making coordination the research claim.** Allocation is
  visible and editable, but users are not required to solve an algorithmic scheduling problem.
- **An inspectable shared representation.** Scene objects, task steps, compound actions, and robot
  assignments remain linked and editable before execution.
- **A practical RoboCasa showcase.** It reuses existing objects and replayable skills, uses two
  homogeneous robots, and avoids dependencies on future scene-editing functionality.

### 2.4 Success condition

The run succeeds when cup 1 and cup 2 are in the sink, both fruits are in the closed refrigerator,
cup 3 remains at its original island location, and every executed task matches the user-confirmed
assignment. For the showcase, the visible evidence is the before/after scene plus the completed
task list; motion optimality, makespan, and collision avoidance are implementation concerns rather
than the primary interaction claim.

---

## 3. Related-Work Positioning

### 3.1 The axes

Two axes describe the landscape; a **third axis is what separates us from same-quadrant HRI
authoring work** (see 3.5).

- **X — Autonomy vs. Steerability:** how much the human can see into and redirect the plan.
  (Left = fully autonomous, goal given, no human control surface. Right = human authors and
  corrects.)
- **Y — Expert vs. Non-expert user:** what the tool assumes of its operator.
  (Bottom = requires MJCF/ROS/programming expertise. Top = usable by a non-expert via
  language/sketch.)
- **Z — Single-agent vs. Multi-agent (team):** whether authoring targets a *single robot* (at any
  level, up to and including goals), or a *team* — which introduces an entire combinatorial
  decision layer (allocation, scheduling, handoff, team composition, cross-agent dependencies)
  absent for a single robot.

### 3.2 Positioning map

```text
        Non-expert user
              ▲
              │           ○ GhostAR (UIST'19)     ★ THIS WORK
              │           ○ Goal-Oriented EUP        (+ multi-agent, full loop,
              │             (HRI'24)                   sim-feedback)
              │             [single-agent] ─────────► separated on Z axis
              │
   Autonomy ──┼─────────────────────────────────► Steerability
 (goal given, │                                  (human authors + corrects)
  no human)   │   ● EMOS            ◆ CoppeliaSim / Isaac Sim
              │   ● RoCo            ◆ (GUI scene editors)
              │
              ▼
          Expert user

  Z axis (into page): single-agent ──► multi-agent (team)
  ○ neighbors share our X–Y quadrant but are single-agent; ★ is multi-agent.
```

- **EMOS / RoCo** — bottom-left: autonomous multi-robot LLM systems. Goal is given by a
  benchmark; the research is robot–robot coordination/allocation. No human authoring surface,
  no non-expert UI.
- **CoppeliaSim / Isaac Sim (and similar GUI editors)** — bottom-right-ish: rich, steerable
  *scene* authoring (drag robots/objects), but assume an **expert**, are single-focus on scene
  construction, and have no LLM task authoring or multi-robot planning loop.
- **GhostAR (UIST'19) / Goal-Oriented EUP (Polaris, HRI'24)** — **same X–Y quadrant as us**
  (non-expert, steerable) but **single-agent**; separated on the Z axis. See 3.5 for the
  substantive difference.
- **This work** — non-expert × steerable × **multi-agent**: agent-assisted **team** task authoring
  across the full loop (scene → allocation → execution → feedback), with multimodal control
  (language + sketch + direct manipulation).

### 3.3 One-line differentiators

| System | User | Agents | Goal source | Human control surface | Loop coverage |
| --- | --- | --- | --- | --- | --- |
| EMOS | (autonomous) | multi | benchmark-given | none | allocation + execution |
| RoCo | (autonomous) | multi | benchmark-given | none | dialogue-based coordination |
| CoppeliaSim / Isaac | expert | any | manual | full but manual, expert-only | scene construction |
| GhostAR (UIST'19) | non-expert | **single** | human-authored | AR embodied demo | one robot's motion/task |
| Goal-Oriented EUP (HRI'24) | non-expert | **single** | human-authored **goal** | goal specification | one robot's goal → plan |
| **This work** | **non-expert** | **multi (team)** | **human-authored intent** | **language + sketch + direct manip.** | **scene → alloc → exec → feedback** |

### 3.4 Framing sentence for the paper

> Autonomous multi-robot planners optimize a **given** goal but give the human no way to inject
> intent or correct the result; expert simulation tools give full manual control but assume
> programming expertise and stop at scene construction. We occupy the gap: **agent-assisted,
> non-expert authoring of multi-robot tasks across the full simulate-and-refine loop.**

### 3.5 The closest neighbors: non-expert robot task authoring (and how we differ)

The sharpest challenge is **not** EMOS/RoCo or GUI editors — it is HRI work that already sits in
the **non-expert × steerable** quadrant: end-user robot task authoring, e.g. *GhostAR: A
Time-space Editor for Embodied Authoring of Human-Robot Collaborative Task with Augmented Reality*
(UIST'19) and *Goal-Oriented End-User Programming of Robots* (system: **Polaris**, HRI'24).
Note the latter is **already goal-oriented for a single robot**, so "we are higher-level than
actions" is *not* the differentiator.

**Crux (vs. Polaris, the HRI'24 system).** Polaris's "goal" is a **closed formal specification**:
the user composes a *goal automaton* from ground **PDDL predicates** (determinate desired world
states), and an off-the-shelf classical planner (Fast Downward) deterministically fills the "how"
for **one** robot; feedback is a **symbolic** plan preview (no simulation, no coordination). It
therefore lives exactly where the outcome is *uniquely determined by the formal goal* — the region
the residual-intent test says to automate. We operate in the complementary region: **open,
under-specified team intent** (preferences, spatial layout via sketch, soft constraints) that
**cannot be reduced to predicates and has no unique satisfying state**, where an LLM proposes a
*team* plan, the human co-steers the "who / when / together / preferences" residual, and **physical
simulation surfaces failures (collisions, deadlocks, failed grasps) that symbolic planning cannot
predict.** In short: *Polaris = formal goal specification for one robot; ours = under-specified
team-intent elicitation with simulation-grounded repair.* ("Goal-oriented" thus means different
things in the two works — formal world-state predicates there, open non-expert intent here.)

This target-level difference is captured by an **authoring abstraction ladder** — rising rungs
leave *more* residual intent to the human, and **L3 → L4 changes the authoring *object*** from a
goal bound to one agent to intent over a team with an **open** coordination structure (which is
why allocation/scheduling/handoff/composition *become* the substance of authoring):

| Rung | Authoring target | Residual the human must supply | Example |
| --- | --- | --- | --- |
| L1 | motion / trajectory | the motion itself (demonstrate) | GhostAR (UIST'19) |
| L2 | action / skill sequence | the ordering of skills | classic EUP / PbD |
| L3 | **single-agent goal** | mostly "how" (automatable planning) | Polaris (HRI'24) |
| **L4** | **team intent (ours)** | **who / when / together / which team / preferences** | **this work** |

*Candidate term for L4:* **team-intent (coordination-level) authoring.**

**Beyond the target level (the crux above), three further differences:**

1. **A combinatorial decision layer single-robot authoring lacks** — allocation, scheduling,
   handoff, team composition, cross-agent dependencies. This is what a non-expert cannot hold in
   their head, so agent assistance / mixed-initiative are *necessary*, not cosmetic.
2. **Repair operates on inter-agent conflicts** — not fixing one action, but resolving deadlocks,
   re-allocating, and re-scheduling, grounded in simulation.
3. **Spatial authoring is coordination in shared space** — sketched routes/zones/arrangements
   coordinate *overlapping* workspaces and paths across agents, not one robot's trajectory.

**Honest scoping.** We do **not** re-claim "non-experts can author robot tasks" or even
"goal-level authoring" (prior single-robot work shows both). Our claim is specifically
**team-intent authoring** — steering a *team*, and the human–agent division of labor over the
multi-agent coordination layer.

---

## Notes / open decisions (to resolve next)

- **North-star vignette** (artifact 2, not yet drafted): one end-to-end story no single prior
  tool can complete. Leaning indoor tidy-up (natural under-specification, non-expert relatable).
- **Scene role:** indoor tidy-up primary (preference/arrangement); warehouse secondary
  (capability allocation / scheduling). Objects are semantic (cups/fruit/goods), not raw cubes.
- **Don't chase Habitat-level realism.** Scenes are a controllable stage for the interaction.
- **Study is the proof.** Every feature must map to something measurable, against a
  no-authoring baseline that visibly fails.
