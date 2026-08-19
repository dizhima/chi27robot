import { describe, expect, it } from "vitest";
import { applyRobotOverrides, type AugmentedAction } from "./authorPlan";
import type { AuthoredPlan } from "./planTypes";

describe("applyRobotOverrides", () => {
  it("round-trips task order and allocation while ignoring compiler tasks", () => {
    const actions: AugmentedAction[] = [
      { id: "a", robot: "robot0", op: "go_to", target: "sink" },
      { id: "b", robot: "robot1", op: "go_to", target: "fridge" },
      { id: "c", robot: "robot0", op: "go_to", target: "counter" },
    ];
    const plan: AuthoredPlan = {
      tasks: [
        { task: "b", robot: "robot1", steps: [{ id: "b0", op: "navigate" }] },
        { task: "robot1#go_to_rest", robot: "robot1", steps: [{ id: "rest", op: "navigate" }] },
        { task: "a", robot: "robot1", robot_locked: true, steps: [{ id: "a0", op: "navigate" }] },
        { task: "c", robot: "robot0", steps: [{ id: "c0", op: "navigate" }] },
      ],
    };

    expect(applyRobotOverrides(actions, plan).map((action) => ({
      id: action.id,
      robot: action.robot,
      locked: action.robot_locked,
    }))).toEqual([
      { id: "b", robot: "robot1", locked: false },
      { id: "a", robot: "robot1", locked: true },
      { id: "c", robot: "robot0", locked: false },
    ]);
  });
});
