/**
 * Pure, immutable edits on an AuthoredPlan. The plan is the single source of
 * truth; every edit returns a NEW plan (never mutates), so it's trivially
 * unit-testable and drives a recompile via usePlanCompile. P1 supports exactly
 * two user edits — reassign a step's robot, and add/remove an `after` dep —
 * plus id assignment on load.
 *
 * Step identity: `after` references and Gantt-bar ↔ step mapping need stable
 * ids, so we assign them up front and preserve any the author/LLM already gave.
 */
import type { AuthoredPlan, AuthoredStep, RobotName } from "./planTypes";

/** All steps in document order, tagged with the robot of their owning task. */
export type FlatStep = AuthoredStep & { robot: RobotName; taskLabel?: string };

/**
 * compound_turn_integration_spec.md §3/D2b: a recorded manual-edit mutation.
 * ScenePage keeps an ordered `edits: PlanEditDelta[]` of PENDING (not yet
 * applied) deltas and sends the whole pending batch with the turn that
 * applies it (an edit-tail sync press, or a chat/append turn per D2b's
 * table). The backend applies them onto whichever plan that turn landed on
 * (`plan_edits.replay_edits`, the Python mirror of this module) -- for an
 * "edit" turn or a D1b pure-append graft, that plan is (or extends) the
 * previous turn's RESOLVED plan, not a fresh `decompose(semanticActions)`:
 * D2's original design rebuilt from scratch every time, which meant a delta
 * targeting a resolver-inserted step (a `go_to_rest` departure leg) had no
 * target and silently vanished. `target` is keyed by whatever survives that:
 * a task-level edit targets the task/action id (`actionId`), a step-level
 * edit falls back to the step id (still a real risk for a batch containing
 * its own stale/contradictory edits -- D2's stated risk, spec §3).
 *
 * `set_step_standoff` is NOT one of the four ops the spec's §3 wire shape
 * enumerates, but `setStepStandoff` is a real `planEdits.*` call site
 * (ScenePage's navigate-standoff drag) and item 16 requires EVERY call site
 * to push a delta so it survives an author turn. It carries no protected-set
 * entry in `buildProtectedSet` below (no resolver tool moves a navigate's
 * standoff), so this is purely a replay-fidelity addition, not a pin source.
 */
export type EditTarget = { actionId?: string; group?: string; stepId?: string };

export type PlanEditDelta =
  | { op: "remove_task"; target: { actionId: string } }
  | { op: "move_task"; target: { actionId: string }; robot: RobotName; afterActionId: string | null }
  | { op: "set_task_robot"; target: EditTarget; robot: RobotName }
  | { op: "set_step_after"; target: EditTarget; after: string[] }
  | { op: "set_step_at"; target: EditTarget; at: [number, number] }
  | { op: "set_step_via"; target: EditTarget; via: number[][] }
  | { op: "set_step_standoff"; target: EditTarget; standoff: [number, number] | null };

/** Wire shape of the resolver's manual-edit protected set (integration spec
 *  §3, `body.protected`). Derived from `edits` (D2: "one structure, two
 *  uses") — never stored separately, always recomputed from the current
 *  edits list + the plan those edits' step ids resolve against. */
export type ProtectedSet = {
  allocations: { group: string; robot: RobotName }[];
  orderings: { before: string; after: string }[];
  /** Exact user-authored step edge. Never inferred from compiler sequencing. */
  exact_after_edges?: { step: string; after_step: string }[];
  /** Complete user-authored `after` field for an edited step, including []. */
  exact_after_fields?: { step: string; after: string[] }[];
  destinations: { step: string }[];
  waypoints: { step: string }[];
};

const EMPTY_PROTECTED_SET: ProtectedSet = {
  allocations: [],
  orderings: [],
  exact_after_edges: [],
  exact_after_fields: [],
  destinations: [],
  waypoints: [],
};

/**
 * Derive the resolver's protected set from the accumulated edit deltas
 * (D2/item 16, "one structure, two uses" — the other use is the backend's
 * `replay_edits`). `plan` supplies the step-id -> task/group lookup needed to
 * turn a step-local `set_step_after` edit into both a TASK-level ordering pin
 * for cheap candidate pruning and an exact step edge for preservation. Pass
 * the plan the edit was made against (`livePlan` is fine — group membership
 * does not change under a manual edit).
 */
export function buildProtectedSet(
  edits: PlanEditDelta[],
  plan: AuthoredPlan | null,
): ProtectedSet {
  if (!edits.length) return EMPTY_PROTECTED_SET;
  const groupOf = new Map<string, string>();
  const ephemeralDetourIds = new Set<string>();
  if (plan) {
    for (const task of plan.tasks) {
      for (const step of task.steps) {
        if (step.id) {
          groupOf.set(step.id, (step.group as string | undefined) ?? task.task ?? step.id);
          if (step.compiler_v2_repair === "detour"
              || step.compiler_v2_repair === "go_away") {
            ephemeralDetourIds.add(step.id);
          }
        }
      }
    }
  }
  // `set_step_after` replaces a whole list. The protected representation must
  // match the final replayed edit state, rather than pinning superseded edges
  // from earlier drags in the same pending batch.
  const finalAfterEdit = new Map<string, PlanEditDelta>();
  const finalMoveEdit = new Map<string, Extract<PlanEditDelta, { op: "move_task" }>>();
  for (const edit of edits) {
    if (edit.op === "move_task") {
      finalMoveEdit.set(edit.target.actionId, edit);
      continue;
    }
    if (edit.op !== "set_step_after") continue;
    const key = edit.target.stepId ? `step:${edit.target.stepId}`
      : edit.target.group ? `group:${edit.target.group}` : null;
    if (key) finalAfterEdit.set(key, edit);
  }
  const out: ProtectedSet = {
    allocations: [], orderings: [], exact_after_edges: [], exact_after_fields: [],
    destinations: [], waypoints: [],
  };
  for (const edit of edits) {
    switch (edit.op) {
      case "remove_task":
        // Removal is applied to semantic actions before decomposition. There
        // is no surviving allocation/order/spatial field to protect.
        break;
      case "move_task": {
        if (finalMoveEdit.get(edit.target.actionId) !== edit) break;
        const original = plan?.tasks.find((task) => task.task === edit.target.actionId);
        if (!original) break;
        if (original.robot !== edit.robot) {
          out.allocations.push({ group: edit.target.actionId, robot: edit.robot });
        }
        if (edit.afterActionId && edit.afterActionId !== edit.target.actionId) {
          out.orderings.push({ before: edit.afterActionId, after: edit.target.actionId });
        }
        break;
      }
      case "set_task_robot": {
        const group = edit.target.actionId ?? edit.target.group;
        if (group) out.allocations.push({ group, robot: edit.robot });
        break;
      }
      case "set_step_after": {
        const targetKey = edit.target.stepId ? `step:${edit.target.stepId}`
          : edit.target.group ? `group:${edit.target.group}` : null;
        if (targetKey && finalAfterEdit.get(targetKey) !== edit) break;
        const stepId = edit.target.stepId;
        const stepGroup = stepId ? groupOf.get(stepId) : edit.target.group;
        if (!stepGroup) break;
        if (stepId) {
          out.exact_after_fields!.push({ step: stepId, after: [...edit.after] });
        }
        for (const afterId of edit.after) {
          const afterGroup = groupOf.get(afterId);
          // Only concrete edges populate the legacy edge pins. The complete
          // field pin above also protects an explicitly cleared [] value;
          // compiler implicit sequencing is never promoted into either form.
          if (stepId && afterId && afterId !== stepId && groupOf.has(afterId)) {
            out.exact_after_edges!.push({ step: stepId, after_step: afterId });
          }
          // The edit means "stepGroup waits for afterGroup" -> the intended
          // order is afterGroup BEFORE stepGroup.
          if (afterGroup && afterGroup !== stepGroup) {
            out.orderings.push({ before: afterGroup, after: stepGroup });
          }
        }
        break;
      }
      case "set_step_at":
        if (edit.target.stepId) out.destinations.push({ step: edit.target.stepId });
        break;
      case "set_step_via":
        if (edit.target.stepId && !ephemeralDetourIds.has(edit.target.stepId)) {
          out.waypoints.push({ step: edit.target.stepId });
        }
        break;
      case "set_step_standoff":
        // No resolver tool moves a navigate's standoff today -- nothing to pin.
        break;
    }
  }
  return out;
}

let idCounter = 0;

/**
 * Ensure every step has a stable `id`. Existing ids are kept (so `after` refs
 * and prior edits survive); missing ones get a fresh `s{n}`. Returns a new plan.
 */
export function ensureStepIds(plan: AuthoredPlan): AuthoredPlan {
  const seen = new Set<string>();
  for (const task of plan.tasks) {
    for (const step of task.steps) {
      if (step.id) seen.add(step.id);
    }
  }
  const freshId = () => {
    let id = `s${idCounter++}`;
    while (seen.has(id)) id = `s${idCounter++}`;
    seen.add(id);
    return id;
  };
  return {
    ...plan,
    tasks: plan.tasks.map((task) => ({
      ...task,
      steps: task.steps.map((step) => (step.id ? step : { ...step, id: freshId() })),
    })),
  };
}

/** Locate a step by id. Returns null if not found. */
function findStep(plan: AuthoredPlan, stepId: string): AuthoredStep | null {
  for (const task of plan.tasks) {
    for (const step of task.steps) {
      if (step.id === stepId) return step;
    }
  }
  return null;
}

/**
 * Reassign a step to another robot. Robot is a task-level property, so the step
 * is moved out of its current task into a task owned by `robot`: it joins that
 * robot's most recent existing task, else a new one is created. Relative order
 * is preserved (appended at the end of the destination). No-op (same plan
 * reference) if the step is already on `robot` or the id is unknown.
 */
export function setStepRobot(
  plan: AuthoredPlan,
  stepId: string,
  robot: RobotName,
): AuthoredPlan {
  const step = findStep(plan, stepId);
  if (!step) return plan;

  const owningTask = plan.tasks.find((t) => t.steps.some((s) => s.id === stepId));
  if (owningTask && owningTask.robot === robot && owningTask.steps.length === 1) {
    return plan; // already solely on the target robot — nothing to move
  }

  // Remove the step from its current task (dropping now-empty tasks).
  const withoutStep = plan.tasks
    .map((t) => ({ ...t, steps: t.steps.filter((s) => s.id !== stepId) }))
    .filter((t) => t.steps.length > 0);

  // Append to the last task already owned by the destination robot, or make one.
  let placed = false;
  const tasks = withoutStep.map((t) => ({ ...t, steps: [...t.steps] }));
  for (let i = tasks.length - 1; i >= 0; i--) {
    if (tasks[i].robot === robot) {
      tasks[i].steps.push(step);
      placed = true;
      break;
    }
  }
  if (!placed) {
    const label = typeof step.group === "string" ? step.group : undefined;
    tasks.push({ task: label, robot, steps: [step] });
  }
  return { ...plan, tasks };
}

/**
 * Reassign the WHOLE task containing `stepId` to `robot` (robot is a semantic,
 * task-level property — a task is one robot's coherent job). Flips that task's
 * `robot`; all its steps move lanes together. No-op if already on `robot` or the
 * id is unknown. Step-level moves are intentionally not offered (use drag for
 * per-step ordering/deps).
 */
export function setTaskRobot(
  plan: AuthoredPlan,
  stepId: string,
  robot: RobotName,
): AuthoredPlan {
  let hit = false;
  const tasks = plan.tasks.map((task) => {
    if (task.robot === robot) return task;
    if (task.steps.some((s) => s.id === stepId)) {
      hit = true;
      return { ...task, robot, robot_locked: true };
    }
    return task;
  });
  return hit ? { ...plan, tasks } : plan;
}

/**
 * First and last AUTHORED step ids of a semantic task, or null when the task is
 * unknown or carries no ids.
 *
 * Task-level dependency edits must resolve through this rather than through
 * compiled Gantt keys. A task bar's first/last compiled step is frequently
 * compiler-generated (`#reposition`, `#detour_`, `#go_to_rest`, `#yield`) and
 * exists in no authored plan, so `setStepAfter` would drop it and return the
 * plan unchanged — an edit that looked queued but meant nothing.
 */
export function taskStepBounds(
  plan: AuthoredPlan,
  actionId: string,
): { first: string; last: string } | null {
  const task = plan.tasks.find((candidate) => candidate.task === actionId);
  const ids = (task?.steps ?? [])
    .map((step) => step.id)
    .filter((id): id is string => typeof id === "string" && id.length > 0);
  if (ids.length === 0) return null;
  return { first: ids[0], last: ids[ids.length - 1] };
}

/**
 * Move one complete semantic task into a robot's program order. `afterActionId`
 * is another task on the destination robot; null means the first slot in that
 * robot's lane. Same-robot moves only reorder. Cross-robot moves also pin the
 * allocation so automated repair cannot hand the task back.
 */
export function moveTask(
  plan: AuthoredPlan,
  actionId: string,
  robot: RobotName,
  afterActionId: string | null,
): AuthoredPlan {
  const sourceIndex = plan.tasks.findIndex((task) => task.task === actionId);
  if (sourceIndex < 0 || afterActionId === actionId) return plan;

  const source = plan.tasks[sourceIndex];
  const anchor = afterActionId
    ? plan.tasks.find((task) => task.task === afterActionId)
    : null;
  if (afterActionId && (!anchor || anchor.robot !== robot)) return plan;

  const remaining = plan.tasks.filter((_, index) => index !== sourceIndex);
  const moved = source.robot === robot
    ? source
    : { ...source, robot, robot_locked: true };
  let insertAt: number;
  if (afterActionId) {
    insertAt = remaining.findIndex((task) => task.task === afterActionId) + 1;
  } else {
    const firstOnRobot = remaining.findIndex((task) => task.robot === robot);
    insertAt = firstOnRobot < 0 ? remaining.length : firstOnRobot;
  }

  const tasks = [...remaining.slice(0, insertAt), moved, ...remaining.slice(insertAt)];
  const unchanged = tasks.length === plan.tasks.length
    && tasks.every((task, index) => task === plan.tasks[index]);
  return unchanged ? plan : { ...plan, tasks };
}

/** Add `afterId` to a step's `after` list (deduped). No-op on unknown/self/dupe. */
export function addAfter(
  plan: AuthoredPlan,
  stepId: string,
  afterId: string,
): AuthoredPlan {
  if (stepId === afterId) return plan;
  const step = findStep(plan, stepId);
  if (!step || !findStep(plan, afterId)) return plan;
  if ((step.after ?? []).includes(afterId)) return plan;
  return mapStep(plan, stepId, (s) => ({ ...s, after: [...(s.after ?? []), afterId] }));
}

/**
 * Replace a step's whole `after` list (deduped, self-refs dropped). The empty
 * array clears the key. Used by drag-to-align, which sets one dependency at a
 * time. No-op on unknown id or when the list is unchanged.
 */
export function setStepAfter(
  plan: AuthoredPlan,
  stepId: string,
  afterIds: string[],
): AuthoredPlan {
  const step = findStep(plan, stepId);
  if (!step) return plan;
  const known = new Set<string>();
  for (const task of plan.tasks) for (const s of task.steps) if (s.id) known.add(s.id);
  const next = [...new Set(afterIds)].filter((a) => a !== stepId && known.has(a));
  const cur = step.after ?? [];
  if (cur.length === next.length && cur.every((a, i) => a === next[i])) return plan;
  return mapStep(plan, stepId, (s) => {
    const out = { ...s };
    if (next.length) out.after = next;
    else delete out.after;
    return out;
  });
}

/**
 * Set a place step's explicit drop point `at` (world XY). The backend recomputes
 * the surface z, so only the 2D point is authored — this is the round-trip hook
 * for dragging the place marker in the 3D scene. Rounded to mm to avoid float
 * noise churning recompiles. No-op on unknown id or an unchanged point.
 */
export function setStepAt(
  plan: AuthoredPlan,
  stepId: string,
  at: [number, number],
): AuthoredPlan {
  const step = findStep(plan, stepId);
  if (!step) return plan;
  const next: [number, number] = [
    Math.round(at[0] * 1000) / 1000,
    Math.round(at[1] * 1000) / 1000,
  ];
  const cur = step.at;
  if (cur && cur[0] === next[0] && cur[1] === next[1]) return plan;
  return mapStep(plan, stepId, (s) => ({ ...s, at: next }));
}

/**
 * Set a navigate step's base standoff (world XY where the robot dwells). The
 * backend keeps the facing (aimed at the target) and only moves the dwell point;
 * null clears the override so the default standoff is used again. Only meaningful
 * for a navigate that isn't feeding an articulation replay (the backend ignores
 * it there). Rounded to mm. No-op on unknown id or an unchanged point.
 */
export function setStepStandoff(
  plan: AuthoredPlan,
  stepId: string,
  xy: [number, number] | null,
): AuthoredPlan {
  const step = findStep(plan, stepId);
  if (!step) return plan;
  const next = xy ? ([Math.round(xy[0] * 1000) / 1000, Math.round(xy[1] * 1000) / 1000] as [number, number]) : null;
  const cur = (Array.isArray(step.standoff) ? (step.standoff as [number, number]) : null) ?? null;
  const same = (cur === null && next === null) || (!!cur && !!next && cur[0] === next[0] && cur[1] === next[1]);
  if (same) return plan;
  return mapStep(plan, stepId, (s) => {
    const out = { ...s };
    if (next) out.standoff = next;
    else delete out.standoff;
    return out;
  });
}

/**
 * Set a navigate step's authored `via_points` constraints. The backend plans
 * and returns a separate full `route`; an empty/null value restores automatic
 * routing. Rounded to mm to avoid float-churn recompiles.
 */
export function setStepViaPoints(
  plan: AuthoredPlan,
  stepId: string,
  route: [number, number][] | null,
): AuthoredPlan {
  const step = findStep(plan, stepId);
  if (!step) return plan;
  const r =
    route && route.length
      ? route.map(([x, y]) => [Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000] as [number, number])
      : null;
  const cur = (Array.isArray(step.via_points) ? step.via_points : null) ?? null;
  const same =
    (cur === null && r === null) ||
    (!!cur && !!r && cur.length === r.length && cur.every((p, i) => p[0] === r[i][0] && p[1] === r[i][1]));
  if (same) return plan;
  return mapStep(plan, stepId, (s) => {
    const out = { ...s };
    if (r) out.via_points = r;
    else delete out.via_points;
    delete out.route;
    return out;
  });
}

/** Remove `afterId` from a step's `after` list. No-op if absent. */
export function removeAfter(
  plan: AuthoredPlan,
  stepId: string,
  afterId: string,
): AuthoredPlan {
  const step = findStep(plan, stepId);
  if (!step || !(step.after ?? []).includes(afterId)) return plan;
  return mapStep(plan, stepId, (s) => {
    const after = (s.after ?? []).filter((a) => a !== afterId);
    const next = { ...s };
    if (after.length) next.after = after;
    else delete next.after;
    return next;
  });
}

/** Apply `fn` to the step with `stepId`, returning a new plan. */
function mapStep(
  plan: AuthoredPlan,
  stepId: string,
  fn: (step: AuthoredStep) => AuthoredStep,
): AuthoredPlan {
  return {
    ...plan,
    tasks: plan.tasks.map((task) => ({
      ...task,
      steps: task.steps.map((step) => (step.id === stepId ? fn(step) : step)),
    })),
  };
}
