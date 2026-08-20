import type { AugmentedAction } from "./authorPlan";
import type { AuthoredPlan, RobotName } from "./planTypes";

const STUDY_ROBOTS: RobotName[] = ["robot0", "robot1"];

/**
 * Identity of one participant-visible semantic task. Deliberately excludes
 * dependency edges, spatial details, lock metadata, and compiler artifacts:
 * Phase 1 scores only task completeness, robot assignment, and per-robot order.
 */
export function studyActionKey(action: AugmentedAction): string {
  return [
    action.op,
    action.object ?? "",
    action.dest ?? "",
    action.facility ?? "",
    action.target ?? "",
  ].join("|");
}

export type StudyPlanState = {
  plan: AuthoredPlan;
  actions: AugmentedAction[];
};

/**
 * Project the realized plan into participant-visible semantic tasks. Plan task
 * order and assignment are authoritative; semantic actions only identify and
 * label those tasks, which filters compiler-generated repair groups.
 */
export function studyTaskSequences({
  plan,
  actions,
}: StudyPlanState): Record<RobotName, string[]> {
  const actionById = new Map(actions.map((action) => [action.id, action]));
  const sequences: Record<RobotName, string[]> = { robot0: [], robot1: [] };
  for (const task of plan.tasks) {
    if (!task.task) continue;
    const action = actionById.get(task.task);
    if (!action) continue;
    sequences[task.robot].push(studyActionKey(action));
  }
  return sequences;
}

export function matchesStudyTarget(
  current: StudyPlanState,
  target: StudyPlanState,
): boolean {
  const currentSequences = studyTaskSequences(current);
  const targetSequences = studyTaskSequences(target);
  return STUDY_ROBOTS.every((robot) => {
    const currentTasks = currentSequences[robot];
    const targetTasks = targetSequences[robot];
    return currentTasks.length === targetTasks.length
      && currentTasks.every((task, index) => task === targetTasks[index]);
  });
}
