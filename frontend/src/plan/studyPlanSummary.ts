import type { AugmentedAction } from "./authorPlan";
import { realizedStudyTasks, type StudyPlanState } from "./studyTargetMatch";

function humanize(value: string | null | undefined): string {
  return (value ?? "").replaceAll("_", " ");
}

export function studyTaskSentence(action: AugmentedAction): string {
  switch (action.op) {
    case "move":
      return `Move ${humanize(action.object)} to ${humanize(action.dest)}`;
    case "open":
      return `Open ${humanize(action.facility)}`;
    case "close":
      return `Close ${humanize(action.facility)}`;
    case "go_to":
      return `Go to ${humanize(action.target)}`;
    default:
      return humanize(action.op);
  }
}

/** Deterministic baseline-only rendering of the realized per-robot task plan. */
export function formatStudyPlanSummary(state: StudyPlanState): string {
  const tasks = realizedStudyTasks(state);
  const sections = (["robot0", "robot1"] as const).map((robot, robotIndex) => {
    const lines = tasks[robot].length > 0
      ? tasks[robot].map((action, index) => `${index + 1}. ${studyTaskSentence(action)}`)
      : ["No tasks"];
    return `Robot ${robotIndex}:\n${lines.join("\n")}`;
  });
  return `Plan updated.\n\nCurrent task specification:\n${sections.join("\n\n")}`;
}
