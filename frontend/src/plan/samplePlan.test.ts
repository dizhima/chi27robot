import { describe, expect, it } from "vitest";
import { SAMPLE_PLAN_FRIDGE_PREPLACE } from "./samplePlan";

describe("fridge pre-place debug plan", () => {
  it("ends at the carrying navigate-to-fridge step and never enters place IK", () => {
    const steps = SAMPLE_PLAN_FRIDGE_PREPLACE.tasks[0].steps;
    expect(steps.at(-1)).toMatchObject({
      id: "preview_nav_fridge",
      op: "navigate",
      target: "fridge",
      standoff: [1.325951, -3.621526],
    });
    expect(steps.some((step) => step.op === "place")).toBe(false);
    expect(steps.some((step) => step.op === "pick" && step.object === "apple_1")).toBe(true);
    expect(steps.some((step) => step.op === "OpenFridge")).toBe(true);
  });
});
