import { describe, expect, it } from "vitest";
import { captureManualEditBaseline, restoreManualEditBaseline } from "./manualEditBaseline";
import type { ManualEditBaseline } from "./manualEditBaseline";

const baseline = (): ManualEditBaseline => ({
  plan: { tasks: [{ task: "move", robot: "robot0", steps: [{ id: "s0", op: "wait" }] }] },
  compile: { schedule: [], warnings: [], conflicts: [], completed: {} },
  semanticActions: [{ id: "move", robot: "robot0", op: "move", object: "apple", dest: "fridge" }],
  messages: [{ id: "m0", role: "user", content: "move the apple" }],
  lastResolverReport: { converged: true },
  versionHistory: { versions: [], currentId: null },
});

describe("manual edit baseline", () => {
  it("captures an immutable checkpoint before the first edit", () => {
    const source = baseline();
    const captured = captureManualEditBaseline(source);
    source.plan.tasks[0].task = "changed";
    source.semanticActions[0].id = "changed";

    expect(captured.plan.tasks[0].task).toBe("move");
    expect(captured.semanticActions[0].id).toBe("move");
  });

  it("returns an isolated payload each time it is restored", () => {
    const captured = captureManualEditBaseline(baseline());
    const first = restoreManualEditBaseline(captured);
    first.plan.tasks[0].task = "edited-after-restore";

    expect(restoreManualEditBaseline(captured).plan.tasks[0].task).toBe("move");
  });
});
