import type { AugmentedAction } from "./authorPlan";
import type { RobotName } from "./planTypes";

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

export function studyTaskSequences(actions: AugmentedAction[]): Record<RobotName, string[]> {
  return Object.fromEntries(
    STUDY_ROBOTS.map((robot) => [
      robot,
      actions.filter((action) => action.robot === robot).map(studyActionKey),
    ]),
  ) as Record<RobotName, string[]>;
}

export function matchesStudyTarget(
  current: AugmentedAction[],
  target: AugmentedAction[],
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
