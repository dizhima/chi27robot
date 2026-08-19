import { describe, expect, it } from "vitest";
import {
  assignPlanTaskHandles,
  planTaskRefFromAction,
  serializePlanRefs,
  type PlanTaskRef,
} from "./planContext";

const move = {
  id: "move_milk_fridge",
  robot: "robot1" as const,
  op: "move" as const,
  object: "milk",
  dest: "fridge",
};

describe("semantic plan task references", () => {
  it("anchors a token to an AugmentedAction id and serializes t-handles in document order", () => {
    const milk = planTaskRefFromAction("token-milk", move);
    const bowl = planTaskRefFromAction("token-bowl", {
      id: "move_bowl_sink",
      robot: "robot0",
      op: "move",
      object: "bowl",
      dest: "sink",
    });
    const handles = assignPlanTaskHandles([bowl, milk]);

    expect(milk).toMatchObject({ kind: "plan_task", actionId: "move_milk_fridge", label: "milk → fridge" });
    expect(handles.get("token-bowl")).toBe("t1");
    expect(handles.get("token-milk")).toBe("t2");
    expect(serializePlanRefs([bowl, milk], handles)).toEqual([
      { kind: "plan_task", handle: "t1", action_id: "move_bowl_sink" },
      { kind: "plan_task", handle: "t2", action_id: "move_milk_fridge" },
    ]);
  });

  it("serializes open and close semantic actions as stable plan references", () => {
    const open = planTaskRefFromAction("token-open", {
      id: "fridge:open",
      robot: "robot0",
      op: "open",
      facility: "fridge",
    });
    const close = planTaskRefFromAction("token-close", {
      id: "fridge:close",
      robot: "robot1",
      op: "close",
      facility: "fridge",
    });

    expect(open.label).toBe("open fridge");
    expect(close.label).toBe("close fridge");
    expect(serializePlanRefs([open, close], assignPlanTaskHandles([open, close]))).toEqual([
      { kind: "plan_task", handle: "t1", action_id: "fridge:open" },
      { kind: "plan_task", handle: "t2", action_id: "fridge:close" },
    ]);
  });

  it("keeps plan refs independent from a pruned token list", () => {
    const refs: PlanTaskRef[] = [
      planTaskRefFromAction("keep", move),
      planTaskRefFromAction("delete", { id: "open_fridge", robot: "robot0", op: "open", facility: "fridge" }),
    ];
    const present = new Set(["keep"]);
    expect(refs.filter((ref) => present.has(ref.id)).map((ref) => ref.actionId)).toEqual(["move_milk_fridge"]);
  });
});
