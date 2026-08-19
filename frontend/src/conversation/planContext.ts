/**
 * Semantic plan-task references.  These intentionally point at an
 * AugmentedAction id, never an execution-step id: a task can be decomposed
 * into several steps and may be re-decomposed after every compound turn.
 */
import type { AugmentedAction } from "../plan/authorPlan";

export type PlanTaskRef = {
  kind: "plan_task";
  /** Composer-token identity (local to the pending message). */
  id: string;
  /** Stable semantic anchor. */
  actionId: string;
  label: string;
  robot: AugmentedAction["robot"];
  op: AugmentedAction["op"];
  object?: string | null;
  dest?: string | null;
  facility?: string | null;
  target?: string | null;
};

export function planTaskLabel(action: AugmentedAction): string {
  switch (action.op) {
    case "move":
      return action.object && action.dest ? `${action.object} → ${action.dest}` : action.id;
    case "open":
      return action.facility ? `open ${action.facility}` : action.id;
    case "close":
      return action.facility ? `close ${action.facility}` : action.id;
    case "go_to":
      return action.target ? `go to ${action.target}` : action.id;
  }
}

export function planTaskRefFromAction(id: string, action: AugmentedAction): PlanTaskRef {
  return {
    kind: "plan_task",
    id,
    actionId: action.id,
    label: planTaskLabel(action),
    robot: action.robot,
    op: action.op,
    object: action.object,
    dest: action.dest,
    facility: action.facility,
    target: action.target,
  };
}

/** Per-message handles follow token/document order, just like scene pin handles. */
export function assignPlanTaskHandles(refs: PlanTaskRef[]): Map<string, string> {
  return new Map(refs.map((ref, index) => [ref.id, `t${index + 1}`]));
}

/** Wire form deliberately remains separate from scene_refs. */
export function serializePlanRefs(refs: PlanTaskRef[], handles?: Map<string, string>): unknown[] {
  return refs.map((ref) => ({
    kind: "plan_task",
    ...(handles?.has(ref.id) ? { handle: handles.get(ref.id) } : {}),
    action_id: ref.actionId,
  }));
}
