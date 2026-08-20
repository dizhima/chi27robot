import { describe, expect, it } from "vitest";
import type { AugmentedAction } from "./authorPlan";
import { formatStudyPlanSummary } from "./studyPlanSummary";

describe("study plan summary", () => {
  it("renders realized plan order and filters compiler-only tasks", () => {
    const actions: AugmentedAction[] = [
      { id: "canned", robot: "robot0", op: "move", object: "canned_food_1", dest: "upper_cabinet" },
      { id: "condiment", robot: "robot0", op: "move", object: "condiment_bottle_1", dest: "upper_cabinet" },
      { id: "open", robot: "robot1", op: "open", facility: "fridge" },
    ];
    const summary = formatStudyPlanSummary({
      actions,
      plan: {
        tasks: [
          { task: "condiment", robot: "robot0", steps: [] },
          { task: "robot0#go_to_rest", robot: "robot0", steps: [] },
          { task: "canned", robot: "robot0", steps: [] },
          { task: "open", robot: "robot1", steps: [] },
        ],
      },
    });

    expect(summary).toBe(
      "Plan updated.\n\nCurrent task specification:\n" +
      "Robot 0:\n1. Move condiment bottle 1 to upper cabinet\n" +
      "2. Move canned food 1 to upper cabinet\n\n" +
      "Robot 1:\n1. Open fridge",
    );
    expect(summary).not.toContain("go to rest");
  });
});
