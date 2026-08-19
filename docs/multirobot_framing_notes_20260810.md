# Multi-Robot Co-Authoring Project — Framing Notes

## 1. Core HCI Positioning

The strongest positioning is not “an LLM interface for multi-robot planning,” nor simply “chat + Gantt + simulation.”

The paper should frame the system as a shift from **autonomous task planning** toward **progressive, plan-contextualized co-authoring of multi-robot plans**.

A useful high-level contrast is:

- **Autonomous planning:** the user specifies the task, and the planner internally decides decomposition, allocation, ordering, and coordination.
- **Progressive co-authoring:** the user can begin with a high-level task, inspect the resulting team plan, and then make local revisions to the evolving plan while the system handles the broader coordination consequences.

A concise positioning statement is:

> Existing LLM-based multi-robot planners primarily treat the user instruction as an upfront task specification and the resulting team plan as an autonomous planning output. We instead treat the generated plan as a persistent interaction context that users can inspect, reference, and progressively revise.

Another concise version is:

> We explore progressive co-authoring of multi-robot plans, where users make local changes to an evolving plan while the system manages their broader coordination consequences.

## 2. Why “Plan Context” Is the Key Mechanism

The system’s main HCI idea is that the generated plan should not disappear behind autonomous planning.
Instead, the plan becomes a **persistent structured interaction context**.

This gives the plan several roles at once:

1. **External representation** — users can see task allocation, ordering, waits, and execution structure.
2. **Editing surface** — users can directly manipulate task assignments, ordering, and spatial details.
3. **Referential context** — users can refer to specific plan elements in later language input.
4. **Persistent machine-readable state** — later LLM interactions can be grounded in structured plan elements rather than requiring the user to restate the relevant context in language.

The planned Gantt-reference interaction is especially important for this framing.
Examples include:

- Select one task and say: “Add the pear after this task.”
- Select two tasks and say: “Swap these two tasks.”
- Select a task and a scene object to express a mixed plan/world reference.

This extends the existing scene-reference idea from **world grounding** (“this cup”) to **plan grounding** (“this task”).

A useful system-level statement is:

> The plan is simultaneously a representation, an editing surface, and conversational context.

## 3. Avoid Over-Relying on “One-Shot” as the Main Framing

EMOS is externally close to one-shot task specification even though its internal agents may reflect, reassign, and iterate.
This distinction is still useful for motivating the work, but “one-shot vs. multi-turn” should not be the top-level HCI contribution.

The stronger distinction is:

> **task-centric autonomous planning vs. plan-contextualized co-authoring**

The baseline can still resemble the EMOS interaction paradigm, but the paper should avoid claiming that the baseline is literally EMOS.
A safer description is:

> An autonomous planning baseline inspired by the interaction paradigm of LLM-based multi-robot planners such as EMOS.

## 4. Baseline Design

The current preferred baseline is implemented using the same system and backend, but removes the structured plan interface.

### Baseline: Autonomous Planning

Keep:

- same LLM
- same planner/compiler/resolver
- same robots and simulator
- same execution capabilities
- same natural-language input
- same scene/world information

Remove:

- Gantt timeline visualization
- structured plan-reference functionality
- direct plan manipulation through the Gantt artifact

The user can still revise the task through natural language, but cannot inspect or directly reference individual structured plan elements.

This makes the experimental manipulation much cleaner than an artificially weakened one-shot baseline.
The comparison becomes:

- **Baseline:** revise the task through language without structured access to the current plan.
- **Ours:** revise through language and/or direct manipulation while the current structured plan is available as visual and referential context.

This avoids the easy criticism that the proposed system only wins because the baseline is not allowed to iterate.

## 5. Relationship to EMOS, LaMMA-P, and RoboCritics

The relevant observation from these systems is that task descriptions are often enumerative at the level of goals or subtasks.
This supports using household tasks composed of multiple object-level goals in the study.

However, requiring users to manually perform robot assignment in the baseline is less appropriate because multi-robot allocation is normally part of the autonomous planner’s job.

Thus, the useful distinction is:

- It is reasonable for users to enumerate **what needs to be achieved**.
- It is less reasonable to force users to enumerate **who should do every subtask** unless that assignment itself is the intended revision.

RoboCritics also suggests a broader automation–control tension, but the present project moves that tension to the level of **multi-robot plan authorship** rather than single-robot program repair.

## 6. Current Paper Story in One Chain

**Prior paradigm**

Task instruction → autonomous decomposition/allocation/coordination → execution

**HCI limitation**

After a plan is generated, users may want to change only a small part of it, but the plan is often not available as structured human-facing context for later interaction.

**Our idea**

Make the evolving plan persistent, visible, directly editable, and referable from language.

**Interaction model**

Specify → inspect → locally revise → re-coordinate → continue refining the same plan

**Multi-robot significance**

The user’s desired change can be local, while its consequences propagate across the team execution.

**Evaluation**

Compare natural-language revision without structured plan context against plan-contextualized co-authoring using repeated small insertion and reordering edits in two household multi-robot tasks.
