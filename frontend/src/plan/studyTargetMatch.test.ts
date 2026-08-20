import { describe, expect, it } from "vitest";
import type { AugmentedAction } from "./authorPlan";
import type { AuthoredPlan, RobotName } from "./planTypes";
import { matchesStudyTarget, type StudyPlanState } from "./studyTargetMatch";

const move = (
  id: string,
  robot: RobotName,
  object: string,
  dest: string,
  after: string[] | null = null,
): AugmentedAction => ({ id, robot, op: "move", object, dest, after });

const actions: AugmentedAction[] = [
  { id: "open", robot: "robot0", op: "open", facility: "fridge" },
  move("lemon", "robot0", "lemon_1", "fridge"),
  move("apple", "robot1", "apple_1", "fridge"),
  { id: "close", robot: "robot0", op: "close", facility: "fridge" },
  move("mug", "robot1", "mug_1", "sink"),
];

const plan = (...tasks: Array<[string, RobotName]>): AuthoredPlan => ({
  tasks: tasks.map(([task, robot]) => ({ task, robot, steps: [] })),
});

const target: StudyPlanState = {
  actions,
  plan: plan(
    ["open", "robot0"],
    ["lemon", "robot0"],
    ["apple", "robot1"],
    ["close", "robot0"],
    ["mug", "robot1"],
  ),
};

describe("study target matching", () => {
  it("uses plan task order while ignoring semantic array order and cross-robot edges", () => {
    const current: StudyPlanState = {
      plan: target.plan,
      actions: [...actions].reverse().map((action) => ({
        ...action,
        after: ["unscored-cross-robot-edge"],
        robot_locked: !action.robot_locked,
      })),
    };
    expect(matchesStudyTarget(current, target)).toBe(true);
  });

  it("filters plan tasks that have no semantic action, such as compiler repairs", () => {
    const current: StudyPlanState = {
      actions,
      plan: {
        tasks: [
          ...target.plan.tasks.slice(0, 2),
          { task: "robot0#go_to_rest", robot: "robot0", steps: [] },
          ...target.plan.tasks.slice(2),
        ],
      },
    };
    expect(matchesStudyTarget(current, target)).toBe(true);
  });

  it("rejects a missing or extra realized plan task", () => {
    const missing: StudyPlanState = {
      actions,
      plan: { tasks: target.plan.tasks.slice(0, -1) },
    };
    const extraAction = move("extra", "robot1", "mug_2", "sink");
    const extra: StudyPlanState = {
      actions: [...actions, extraAction],
      plan: { tasks: [...target.plan.tasks, { task: "extra", robot: "robot1", steps: [] }] },
    };
    expect(matchesStudyTarget(missing, target)).toBe(false);
    expect(matchesStudyTarget(extra, target)).toBe(false);
  });

  it("uses the realized plan's robot assignment", () => {
    const current: StudyPlanState = {
      actions,
      plan: {
        tasks: target.plan.tasks.map((task) =>
          task.task === "apple" ? { ...task, robot: "robot0" } : task,
        ),
      },
    };
    expect(matchesStudyTarget(current, target)).toBe(false);
  });

  it("rejects a wrong order within either robot", () => {
    const current: StudyPlanState = {
      actions,
      plan: plan(
        ["open", "robot0"],
        ["close", "robot0"],
        ["lemon", "robot0"],
        ["apple", "robot1"],
        ["mug", "robot1"],
      ),
    };
    expect(matchesStudyTarget(current, target)).toBe(false);
  });
});
