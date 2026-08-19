import { describe, expect, it } from "vitest";
import {
  addAfter,
  buildProtectedSet,
  ensureStepIds,
  moveTask,
  removeAfter,
  setStepAfter,
  setStepAt,
  setStepRobot,
  setStepStandoff,
  setStepViaPoints,
  setTaskRobot,
  taskStepBounds,
} from "./planEdits";
import type { AuthoredPlan } from "./planTypes";

const plan = (): AuthoredPlan => ({
  tasks: [
    {
      task: "mug_1 to sink",
      robot: "robot0",
      steps: [
        { id: "a", op: "navigate", target: "mug_1" },
        { id: "b", op: "place", object: "mug_1", dest: "sink" },
      ],
    },
    {
      task: "mug_2 to sink",
      robot: "robot1",
      steps: [{ id: "c", op: "navigate", target: "sink" }],
    },
  ],
});

const stepIds = (p: AuthoredPlan) => p.tasks.flatMap((t) => t.steps.map((s) => s.id));
const robotOf = (p: AuthoredPlan, id: string) =>
  p.tasks.find((t) => t.steps.some((s) => s.id === id))?.robot;

describe("taskStepBounds", () => {
  it("resolves a semantic task to its authored first and last step", () => {
    expect(taskStepBounds(plan(), "mug_1 to sink")).toEqual({ first: "a", last: "b" });
    expect(taskStepBounds(plan(), "mug_2 to sink")).toEqual({ first: "c", last: "c" });
  });

  it("returns null for an unknown task or one with no authored ids", () => {
    expect(taskStepBounds(plan(), "robot0#go_to_rest")).toBeNull();
    expect(taskStepBounds(
      { tasks: [{ task: "t", robot: "robot0", steps: [{ op: "wait", duration: 1 }] }] },
      "t",
    )).toBeNull();
  });

  it("feeds setStepAfter an id it will actually accept", () => {
    const source = plan();
    const dragged = taskStepBounds(source, "mug_2 to sink")!;
    const target = taskStepBounds(source, "mug_1 to sink")!;
    const edited = setStepAfter(source, dragged.first, [target.last]);

    // The compiled Gantt key for the same task tail is often a generated id;
    // that path returns the plan untouched, which is the bug this guards.
    expect(edited).not.toBe(source);
    expect(edited.tasks[1].steps[0].after).toEqual(["b"]);
    expect(setStepAfter(source, dragged.first, ["b#reposition3"])).toBe(source);
  });
});

describe("ensureStepIds", () => {
  it("keeps existing ids and only fills missing ones", () => {
    const p: AuthoredPlan = {
      tasks: [{ robot: "robot0", steps: [{ id: "keep", op: "pick", object: "x" }, { op: "wait", duration: 1 }] }],
    };
    const out = ensureStepIds(p);
    const ids = stepIds(out);
    expect(ids[0]).toBe("keep");
    expect(ids[1]).toBeTruthy();
    expect(new Set(ids).size).toBe(2); // unique
  });

  it("does not mutate the input plan", () => {
    const p: AuthoredPlan = { tasks: [{ robot: "robot0", steps: [{ op: "wait", duration: 1 }] }] };
    ensureStepIds(p);
    expect(p.tasks[0].steps[0].id).toBeUndefined();
  });
});

describe("setStepRobot", () => {
  it("moves a step to the other robot and preserves siblings", () => {
    const out = setStepRobot(plan(), "b", "robot1");
    expect(robotOf(out, "b")).toBe("robot1");
    expect(robotOf(out, "a")).toBe("robot0"); // sibling stays
    expect(stepIds(out).sort()).toEqual(["a", "b", "c"]); // nothing lost
  });

  it("drops a task that becomes empty after moving its only step", () => {
    const out = setStepRobot(plan(), "c", "robot0");
    expect(out.tasks.every((t) => t.robot !== "robot1")).toBe(true);
    expect(robotOf(out, "c")).toBe("robot0");
  });

  it("is a no-op when the step is already alone on the target robot", () => {
    const p = plan();
    expect(setStepRobot(p, "c", "robot1")).toBe(p);
  });

  it("is a no-op for an unknown id", () => {
    const p = plan();
    expect(setStepRobot(p, "nope", "robot1")).toBe(p);
  });
});

describe("setTaskRobot", () => {
  it("flips the whole task's robot, moving all its steps together", () => {
    const out = setTaskRobot(plan(), "a", "robot1"); // task "mug_1 to sink" has a + b
    const task = out.tasks.find((t) => t.steps.some((s) => s.id === "a"));
    expect(task?.robot).toBe("robot1");
    expect(task?.robot_locked).toBe(true);
    expect(task?.steps.map((s) => s.id)).toEqual(["a", "b"]); // b came along
  });

  it("is a no-op when the task is already on the target robot", () => {
    const p = plan();
    expect(setTaskRobot(p, "a", "robot0")).toBe(p);
  });

  it("is a no-op for an unknown id", () => {
    const p = plan();
    expect(setTaskRobot(p, "nope", "robot1")).toBe(p);
  });
});

describe("moveTask", () => {
  const orderedPlan = (): AuthoredPlan => ({
    tasks: [
      { task: "a", robot: "robot0", steps: [{ id: "a0", op: "wait", duration: 1 }] },
      { task: "b", robot: "robot1", steps: [{ id: "b0", op: "wait", duration: 1 }] },
      { task: "c", robot: "robot0", steps: [{ id: "c0", op: "wait", duration: 1 }] },
      { task: "d", robot: "robot1", steps: [{ id: "d0", op: "wait", duration: 1 }] },
    ],
  });

  it("reorders a task within the same robot program", () => {
    const input = orderedPlan();
    const out = moveTask(input, "a", "robot0", "c");
    expect(out.tasks.map((task) => task.task)).toEqual(["b", "c", "a", "d"]);
    expect(out.tasks.find((task) => task.task === "a")?.robot_locked).toBeUndefined();
    expect(input.tasks.map((task) => task.task)).toEqual(["a", "b", "c", "d"]);
  });

  it("reassigns and inserts a whole task across robots", () => {
    const out = moveTask(orderedPlan(), "a", "robot1", "b");
    expect(out.tasks.map((task) => task.task)).toEqual(["b", "a", "c", "d"]);
    expect(out.tasks.find((task) => task.task === "a")).toMatchObject({
      robot: "robot1",
      robot_locked: true,
    });
  });

  it("uses null as the first slot and rejects an anchor on another robot", () => {
    expect(moveTask(orderedPlan(), "d", "robot1", null).tasks.map((task) => task.task))
      .toEqual(["a", "d", "b", "c"]);
    const input = orderedPlan();
    expect(moveTask(input, "a", "robot1", "c")).toBe(input);
  });
});

describe("addAfter / removeAfter", () => {
  const afterOf = (p: AuthoredPlan, id: string) =>
    p.tasks.flatMap((t) => t.steps).find((s) => s.id === id)?.after;

  it("adds a dependency", () => {
    const out = addAfter(plan(), "c", "b");
    expect(afterOf(out, "c")).toEqual(["b"]);
  });

  it("dedupes and rejects self-reference and unknown targets", () => {
    let out = addAfter(plan(), "c", "b");
    out = addAfter(out, "c", "b"); // dup
    expect(afterOf(out, "c")).toEqual(["b"]);
    expect(addAfter(plan(), "c", "c")).toEqual(plan()); // self
    expect(addAfter(plan(), "c", "ghost")).toEqual(plan()); // unknown
  });

  it("removes a dependency and deletes the empty after key", () => {
    const withDep = addAfter(plan(), "c", "b");
    const out = removeAfter(withDep, "c", "b");
    expect(afterOf(out, "c")).toBeUndefined();
  });

  it("does not mutate the input", () => {
    const p = plan();
    addAfter(p, "c", "b");
    expect(afterOf(p, "c")).toBeUndefined();
  });
});

describe("setStepAfter", () => {
  const afterOf = (p: AuthoredPlan, id: string) =>
    p.tasks.flatMap((t) => t.steps).find((s) => s.id === id)?.after;

  it("replaces the whole after list", () => {
    const out = setStepAfter(addAfter(plan(), "c", "a"), "c", ["b"]);
    expect(afterOf(out, "c")).toEqual(["b"]);
  });

  it("clears the key with an empty list", () => {
    const out = setStepAfter(addAfter(plan(), "c", "b"), "c", []);
    expect(afterOf(out, "c")).toBeUndefined();
  });

  it("drops self-refs, unknown ids, and dupes", () => {
    const out = setStepAfter(plan(), "c", ["c", "ghost", "b", "b", "a"]);
    expect(afterOf(out, "c")).toEqual(["b", "a"]);
  });

  it("is a no-op when unchanged", () => {
    const withDep = addAfter(plan(), "c", "b");
    expect(setStepAfter(withDep, "c", ["b"])).toBe(withDep);
  });
});

describe("buildProtectedSet", () => {
  it("pins the final task move's allocation and ordering", () => {
    const protectedSet = buildProtectedSet([
      { op: "move_task", target: { actionId: "mug_1 to sink" }, robot: "robot1", afterActionId: "mug_2 to sink" },
    ], plan());
    expect(protectedSet.allocations).toEqual([
      { group: "mug_1 to sink", robot: "robot1" },
    ]);
    expect(protectedSet.orderings).toEqual([
      { before: "mug_2 to sink", after: "mug_1 to sink" },
    ]);
  });

  it("does not create resolver pins for a semantic task removal", () => {
    expect(buildProtectedSet([
      { op: "remove_task", target: { actionId: "mug_1 to sink" } },
    ], plan())).toEqual({
      allocations: [],
      orderings: [],
      exact_after_edges: [],
      exact_after_fields: [],
      destinations: [],
      waypoints: [],
    });
  });

  it("preserves each non-empty manual after edit as an exact step edge", () => {
    const protectedSet = buildProtectedSet([
      { op: "set_step_after", target: { stepId: "b" }, after: ["c"] },
    ], plan());

    expect(protectedSet.exact_after_edges).toEqual([
      { step: "b", after_step: "c" },
    ]);
    expect(protectedSet.exact_after_fields).toEqual([
      { step: "b", after: ["c"] },
    ]);
    // Keep the existing group-level ordering pin as inexpensive pruning.
    expect(protectedSet.orderings).toEqual([
      { before: "mug_2 to sink", after: "mug_1 to sink" },
    ]);
  });

  it("locks an empty after field when the user clears after", () => {
    const protectedSet = buildProtectedSet([
      { op: "set_step_after", target: { stepId: "b" }, after: [] },
    ], plan());

    expect(protectedSet.exact_after_edges).toEqual([]);
    expect(protectedSet.exact_after_fields).toEqual([
      { step: "b", after: [] },
    ]);
  });

  it("pins only the final set_step_after state for a step", () => {
    const protectedSet = buildProtectedSet([
      { op: "set_step_after", target: { stepId: "b" }, after: ["a"] },
      { op: "set_step_after", target: { stepId: "b" }, after: ["c"] },
    ], plan());

    expect(protectedSet.exact_after_edges).toEqual([
      { step: "b", after_step: "c" },
    ]);
    expect(protectedSet.exact_after_fields).toEqual([
      { step: "b", after: ["c"] },
    ]);

    const cleared = buildProtectedSet([
      { op: "set_step_after", target: { stepId: "b" }, after: ["c"] },
      { op: "set_step_after", target: { stepId: "b" }, after: [] },
    ], plan());
    expect(cleared.exact_after_edges).toEqual([]);
    expect(cleared.exact_after_fields).toEqual([
      { step: "b", after: [] },
    ]);
    expect(cleared.orderings).toEqual([]);
  });

  it("does not promote an edited compiler detour into a waypoint pin", () => {
    const withDetour: AuthoredPlan = {
      tasks: [{
        task: "blocked:s2#detour_robot1",
        robot: "robot1",
        steps: [{
          id: "blocked:s2#detour_robot1",
          op: "navigate",
          compiler_v2_repair: "detour",
        }],
      }],
    };
    const protectedSet = buildProtectedSet([{
      op: "set_step_via",
      target: { stepId: "blocked:s2#detour_robot1" },
      via: [[1, 2]],
    }], withDetour);

    expect(protectedSet.waypoints).toEqual([]);
  });
});

describe("setStepAt", () => {
  const atOf = (p: AuthoredPlan, id: string) =>
    p.tasks.flatMap((t) => t.steps).find((s) => s.id === id)?.at;

  it("sets the drop point (rounded to mm)", () => {
    const out = setStepAt(plan(), "b", [1.23456, -4.0]);
    expect(atOf(out, "b")).toEqual([1.235, -4]);
  });

  it("is a no-op on an unknown id", () => {
    const p = plan();
    expect(setStepAt(p, "ghost", [1, 2])).toBe(p);
  });

  it("is a no-op when the point is unchanged", () => {
    const withAt = setStepAt(plan(), "b", [1.235, -4]);
    expect(setStepAt(withAt, "b", [1.235, -4])).toBe(withAt);
  });
});

describe("setStepViaPoints", () => {
  const wpOf = (p: AuthoredPlan, id: string) =>
    p.tasks.flatMap((t) => t.steps).find((s) => s.id === id)?.via_points;

  it("sets the route (rounded to mm)", () => {
    const out = setStepViaPoints(plan(), "a", [[1.1119, 2.0], [3.0, 4.0]]);
    expect(wpOf(out, "a")).toEqual([[1.112, 2], [3, 4]]);
  });

  it("drops the key on an empty route (revert to auto-route)", () => {
    const withWp = setStepViaPoints(plan(), "a", [[1, 2]]);
    const out = setStepViaPoints(withWp, "a", []);
    expect(wpOf(out, "a")).toBeUndefined();
    expect("via_points" in out.tasks[0].steps[0]).toBe(false);
  });

  it("drops the key on null", () => {
    const withWp = setStepViaPoints(plan(), "a", [[1, 2]]);
    expect(wpOf(setStepViaPoints(withWp, "a", null), "a")).toBeUndefined();
  });

  it("is a no-op on an unknown id", () => {
    const p = plan();
    expect(setStepViaPoints(p, "ghost", [[1, 2]])).toBe(p);
  });

  it("is a no-op when the route is unchanged", () => {
    const withWp = setStepViaPoints(plan(), "a", [[1, 2], [3, 4]]);
    expect(setStepViaPoints(withWp, "a", [[1, 2], [3, 4]])).toBe(withWp);
  });
});

describe("setStepStandoff", () => {
  const soOf = (p: AuthoredPlan, id: string) =>
    p.tasks.flatMap((t) => t.steps).find((s) => s.id === id)?.standoff;

  it("sets the dwell point (rounded to mm)", () => {
    const out = setStepStandoff(plan(), "a", [3.29999, -3.7]);
    expect(soOf(out, "a")).toEqual([3.3, -3.7]);
  });

  it("drops the key on null (revert to default standoff)", () => {
    const withSo = setStepStandoff(plan(), "a", [3.3, -3.7]);
    const out = setStepStandoff(withSo, "a", null);
    expect(soOf(out, "a")).toBeUndefined();
    expect("standoff" in out.tasks[0].steps[0]).toBe(false);
  });

  it("is a no-op on an unknown id", () => {
    const p = plan();
    expect(setStepStandoff(p, "ghost", [1, 2])).toBe(p);
  });

  it("is a no-op when unchanged", () => {
    const withSo = setStepStandoff(plan(), "a", [3.3, -3.7]);
    expect(setStepStandoff(withSo, "a", [3.3, -3.7])).toBe(withSo);
  });
});
