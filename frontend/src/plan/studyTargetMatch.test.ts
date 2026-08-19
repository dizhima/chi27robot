import { describe, expect, it } from "vitest";
import type { AugmentedAction } from "./authorPlan";
import { matchesStudyTarget } from "./studyTargetMatch";

const move = (
  id: string,
  robot: "robot0" | "robot1",
  object: string,
  dest: string,
  after: string[] | null = null,
): AugmentedAction => ({ id, robot, op: "move", object, dest, after });

const target: AugmentedAction[] = [
  { id: "open", robot: "robot0", op: "open", facility: "fridge" },
  move("lemon", "robot0", "lemon_1", "fridge"),
  move("apple", "robot1", "apple_1", "fridge"),
  { id: "close", robot: "robot0", op: "close", facility: "fridge" },
  move("mug", "robot1", "mug_1", "sink"),
];

describe("study target matching", () => {
  it("matches complete per-robot task sequences while ignoring cross-robot edges", () => {
    const current = target.map((action) => ({
      ...action,
      id: `different-${action.id}`,
      after: ["unscored-cross-robot-edge"],
      robot_locked: !action.robot_locked,
    }));
    expect(matchesStudyTarget(current, target)).toBe(true);
  });

  it("rejects a missing or extra task", () => {
    expect(matchesStudyTarget(target.slice(0, -1), target)).toBe(false);
    expect(matchesStudyTarget([...target, move("extra", "robot1", "mug_2", "sink")], target)).toBe(false);
  });

  it("rejects a wrong robot assignment", () => {
    const current = target.map((action) =>
      action.id === "apple" ? { ...action, robot: "robot0" as const } : action,
    );
    expect(matchesStudyTarget(current, target)).toBe(false);
  });

  it("rejects a wrong order within either robot", () => {
    const current = [target[0], target[3], target[1], target[2], target[4]];
    expect(matchesStudyTarget(current, target)).toBe(false);
  });
});
