import { describe, expect, it } from "vitest";
import {
  aggregateToTaskBars,
  isSupportTaskBar,
  mergeResetBars,
  prettyGroupFallback,
  prettyStepLabel,
  prettyTaskLabel,
  projectTaskAfterBars,
  projectTaskMoveBars,
  toGanttBars,
  type GanttBar,
} from "./ganttModel";
import type { AugmentedAction } from "./authorPlan";

const bar = (over: Partial<GanttBar> & { key: string; robot: string; start: number; duration: number }): GanttBar => ({
  label: over.key,
  warned: false,
  ...over,
});

describe("projectTaskMoveBars", () => {
  const compiled = [
    bar({ key: "a-step", group: "a", robot: "robot0", start: 0, duration: 10 }),
    bar({ key: "b-step", group: "b", robot: "robot0", start: 10, duration: 20 }),
    bar({ key: "c-step", group: "c", robot: "robot1", start: 0, duration: 7 }),
    bar({ key: "d-step", group: "d", robot: "robot1", start: 7, duration: 5 }),
  ];

  it("keeps compiled widths, repacks both lanes, and returns the origin ghost", () => {
    const result = projectTaskMoveBars(compiled, [
      { actionId: "c", sourceRobot: "robot1", robot: "robot0", afterActionId: "a" },
    ]);

    expect(result.bars.map((b) => [b.group, b.robot, b.start, b.duration])).toEqual([
      ["a", "robot0", 0, 10],
      ["c", "robot0", 10, 7],
      ["b", "robot0", 17, 20],
      ["d", "robot1", 7, 5],
    ]);
    expect(result.ghostBars).toEqual([compiled[2]]);
  });

  it("replays repeated moves but keeps one ghost at the compiled position", () => {
    const result = projectTaskMoveBars(compiled, [
      { actionId: "b", sourceRobot: "robot0", robot: "robot1", afterActionId: "d" },
      { actionId: "b", sourceRobot: "robot1", robot: "robot1", afterActionId: "c" },
    ]);

    expect(result.bars.find((b) => b.group === "b")).toMatchObject({
      robot: "robot1",
      start: 7,
      duration: 20,
    });
    expect(result.ghostBars).toEqual([compiled[1]]);
  });

  it("leaves untouched lanes at their compiled positions", () => {
    const delayed = [
      ...compiled,
      bar({ key: "e-step", group: "e", robot: "robot2", start: 42, duration: 9 }),
    ];
    const result = projectTaskMoveBars(delayed, [
      { actionId: "a", sourceRobot: "robot0", robot: "robot1", afterActionId: "c" },
    ]);

    expect(result.bars.find((b) => b.group === "e")).toMatchObject({ start: 42, duration: 9 });
  });

  it("reorders within one robot without creating a ghost", () => {
    const result = projectTaskMoveBars(compiled, [
      { actionId: "a", sourceRobot: "robot0", robot: "robot0", afterActionId: "b" },
    ]);

    expect(result.bars.filter((b) => b.robot === "robot0").map((b) => [b.group, b.start])).toEqual([
      ["b", 0],
      ["a", 20],
    ]);
    expect(result.ghostBars).toEqual([]);
  });

});

describe("projectTaskAfterBars", () => {
  const compiled = [
    bar({ key: "a-step", group: "a", robot: "robot0", start: 0, duration: 10 }),
    bar({ key: "b-step", group: "b", robot: "robot0", start: 10, duration: 8 }),
    bar({ key: "c-step", group: "c", robot: "robot1", start: 0, duration: 25 }),
  ];

  it("pulls the waiting task's left edge to the anchor's right edge", () => {
    const result = projectTaskAfterBars(compiled, [
      { actionId: "a", afterActionId: "c" },
    ]);

    const waiter = result.find((b) => b.group === "a")!;
    const anchor = result.find((b) => b.group === "c")!;
    expect(waiter.start).toBe(anchor.start + anchor.duration);
    expect(waiter.robot).toBe("robot0");
    expect(waiter.duration).toBe(10);
    expect(waiter.pendingAfter).toBe("waiter");
    expect(anchor.pendingAfter).toBe("anchor");
  });

  it("pushes the waiter's own lane suffix out of the way", () => {
    const result = projectTaskAfterBars(compiled, [
      { actionId: "a", afterActionId: "c" },
    ]);

    expect(result.filter((b) => b.robot === "robot0").map((b) => [b.group, b.start])).toEqual([
      ["a", 25],
      ["b", 35],
    ]);
  });

  it("never pulls a task earlier than its anchor or its own lane predecessor", () => {
    const alreadyLater = [
      bar({ key: "x-step", group: "x", robot: "robot0", start: 40, duration: 5 }),
      bar({ key: "y-step", group: "y", robot: "robot1", start: 0, duration: 3 }),
    ];
    const result = projectTaskAfterBars(alreadyLater, [
      { actionId: "x", afterActionId: "y" },
    ]);

    expect(result.find((b) => b.group === "x")).toMatchObject({ start: 40 });
    // The pair is still highlighted: the edit is real, it just changes no time.
    expect(result.find((b) => b.group === "x")?.pendingAfter).toBe("waiter");
  });

  it("leaves the input untouched and ignores unresolvable ends", () => {
    const result = projectTaskAfterBars(compiled, [
      { actionId: "a", afterActionId: "missing" },
    ]);

    expect(compiled[0].start).toBe(0);
    expect(compiled[0].pendingAfter).toBeUndefined();
    expect(result.find((b) => b.group === "a")).toMatchObject({ start: 0 });
  });
});

describe("aggregateToTaskBars", () => {
  const bars: GanttBar[] = [
    bar({ key: "a", robot: "robot0", group: "mug_1 to sink", start: 0, duration: 10 }),
    bar({ key: "b", robot: "robot0", group: "mug_1 to sink", start: 10, duration: 5, warned: true }),
    bar({ key: "c", robot: "robot0", group: "open fridge", start: 15, duration: 20 }),
    bar({ key: "d", robot: "robot1", group: "mug_2 to sink", start: 0, duration: 6 }),
  ];

  it("collapses same-group steps into one bar spanning their range", () => {
    const tasks = aggregateToTaskBars(bars);
    expect(tasks.map((t) => t.label)).toEqual(["mug_1 to sink", "open fridge", "mug_2 to sink"]);
    const t0 = tasks[0];
    expect(t0.start).toBe(0);
    expect(t0.duration).toBe(15); // 0 -> max end (15)
    expect(t0.warned).toBe(true); // any member warned
    expect(t0.memberKeys).toEqual(["a", "b"]);
    expect(t0.key).toBe("a"); // first step, so a click resolves to the task
    expect(t0.endStepKey).toBe("b"); // last-finishing step = the after anchor
  });

  it("keeps different robots' same-named work separate", () => {
    const tasks = aggregateToTaskBars(bars);
    expect(tasks.filter((t) => t.robot === "robot1")).toHaveLength(1);
  });

  it("uses the pretty label from actionsById when given, keeping key/group as the action id", () => {
    const actionsById = new Map<string, AugmentedAction>([
      ["mug_1 to sink", { id: "mug_1 to sink", robot: "robot0", op: "move", object: "mug_1", dest: "sink" }],
    ]);
    const tasks = aggregateToTaskBars(bars, actionsById);
    const t0 = tasks[0];
    expect(t0.label).toBe("mug_1 → sink");
    expect(t0.key).toBe("a");
    expect(t0.group).toBe("mug_1 to sink");
    // Groups with no matching action keep falling back to the raw group/label.
    expect(tasks[1].label).toBe("open fridge");
  });
});

describe("prettyTaskLabel", () => {
  const base = { id: "act1", robot: "robot0" as const };

  it("formats move as 'object → dest'", () => {
    expect(prettyTaskLabel({ ...base, op: "move", object: "apple_1", dest: "fridge" })).toBe(
      "apple_1 → fridge",
    );
  });

  it("formats open as 'open facility'", () => {
    expect(prettyTaskLabel({ ...base, op: "open", facility: "fridge" })).toBe("open fridge");
  });

  it("formats close as 'close facility'", () => {
    expect(prettyTaskLabel({ ...base, op: "close", facility: "fridge" })).toBe("close fridge");
  });

  it("formats go_to as 'go to target'", () => {
    expect(prettyTaskLabel({ ...base, op: "go_to", target: "counter" })).toBe("go to counter");
  });

  it("falls back to the action id when required fields are missing", () => {
    expect(prettyTaskLabel({ ...base, op: "move", object: "apple_1" })).toBe("act1");
    expect(prettyTaskLabel({ ...base, op: "open" })).toBe("act1");
  });

  it("falls back to the action id for an unknown op", () => {
    expect(prettyTaskLabel({ ...base, op: "wait" as AugmentedAction["op"] })).toBe("act1");
  });

  it("returns an empty string for a missing action", () => {
    expect(prettyTaskLabel(null)).toBe("");
    expect(prettyTaskLabel(undefined)).toBe("");
  });
});

describe("prettyGroupFallback", () => {
  it("prettifies the resolver's go_to_rest departure anchor", () => {
    expect(prettyGroupFallback("robot0#go_to_rest")).toBe("go to rest");
    expect(prettyGroupFallback("robot1#go_to_rest")).toBe("go to rest");
  });

  it("labels the resolver's deadlock yield task as detour", () => {
    expect(prettyGroupFallback("a_nav#yield")).toBe("detour");
  });

  it("labels Compiler V2's generated parking task as detour", () => {
    expect(prettyGroupFallback("move_apple_1_fridge:s2#detour_robot1")).toBe("detour");
  });

  it("leaves unrecognized groups unchanged", () => {
    expect(prettyGroupFallback("move_mug_1_sink")).toBe("move_mug_1_sink");
  });
});

describe("prettyStepLabel", () => {
  it("uses compact operation names for object motion and detours", () => {
    expect(prettyStepLabel({ op: "navigate" })).toBe("navigate");
    expect(prettyStepLabel({ op: "pick" })).toBe("pick");
    expect(prettyStepLabel({ op: "place" })).toBe("place");
    expect(prettyStepLabel({ op: "navigate", repairKind: "detour" }))
      .toBe("navigate");
  });

  it("keeps the facility on open and close steps", () => {
    expect(prettyStepLabel({ op: "OpenFridge", facility: "fridge" }))
      .toBe("open fridge");
    expect(prettyStepLabel({ op: "CloseUpperCabinet" }))
      .toBe("close upper cabinet");
  });
});

describe("aggregateToTaskBars go_to_rest fallback", () => {
  it("labels a resolver departure bar without an action via the group fallback", () => {
    const bars: GanttBar[] = [
      { key: "r0:rest", robot: "robot0", label: "reset", start: 0, duration: 2, group: "robot0#go_to_rest", op: "reset", warned: false },
    ];
    expect(aggregateToTaskBars(bars).map((b) => b.label)).toEqual(["go to rest"]);
  });
});

describe("aggregateToTaskBars deadlock-yield fallback", () => {
  it("uses the short detour label while preserving the internal group", () => {
    const bars: GanttBar[] = [
      { key: "r0:yield", robot: "robot0", label: "navigate_rest", start: 0, duration: 2, group: "a_nav#yield", op: "navigate", warned: false },
    ];
    expect(aggregateToTaskBars(bars)[0]).toMatchObject({
      label: "detour",
      group: "a_nav#yield",
      tone: "primary",
    });
  });
});

describe("Compiler V2 detour task projection", () => {
  it("labels the step detour and folds it into its semantic task bar", () => {
    const group = "move_apple_1_fridge:s2#detour_robot1";
    const semanticGroup = "move_apple_2_fridge";
    const stepBars = toGanttBars([{
      id: group,
      robot: "robot1",
      label: `navigate_${group}`,
      op: "navigate",
      start: 10,
      duration: 2,
      group,
      facility: null,
      object: null,
      after: [],
      source: "compiler_v2",
      repair_kind: "detour",
      parent_group: semanticGroup,
      track_url: "/tracks/detour.json",
    }, {
      id: "apple2:s0",
      robot: "robot1",
      label: "navigate_apple_2",
      op: "navigate",
      start: 12,
      duration: 8,
      group: semanticGroup,
      facility: null,
      object: "apple_2",
      after: [],
      track_url: "/tracks/apple2.json",
    }], new Set());

    expect(stepBars[0]).toMatchObject({ key: group, group, label: "navigate" });
    const actions = new Map<string, AugmentedAction>([[semanticGroup, {
      id: semanticGroup,
      robot: "robot1",
      op: "move",
      object: "apple_2",
      dest: "fridge",
    }]]);
    expect(aggregateToTaskBars(stepBars, actions)).toEqual([expect.objectContaining({
      key: "apple2:s0",
      group: semanticGroup,
      label: "apple_2 → fridge",
      start: 10,
      duration: 10,
      memberKeys: [group, "apple2:s0"],
      tone: "primary",
    })]);
  });
});

describe("aggregateToTaskBars task tones", () => {
  it("separates object-moving work from open/close support work", () => {
    const bars: GanttBar[] = [
      { key: "move:nav", robot: "robot0", label: "navigate_mug", start: 0, duration: 2, group: "move_mug", op: "navigate", warned: false },
      { key: "open:nav", robot: "robot0", label: "navigate_fridge", start: 2, duration: 2, group: "open_fridge", op: "navigate", warned: false },
      { key: "open:act", robot: "robot0", label: "OpenFridge", start: 4, duration: 2, group: "open_fridge", op: "OpenFridge", warned: false },
    ];
    const result = aggregateToTaskBars(bars);
    expect(result.map((bar) => [bar.group, bar.tone])).toEqual([
      ["move_mug", "primary"],
      ["open_fridge", "support"],
    ]);
  });

  it("recognizes all four support task families from op/group metadata", () => {
    const base = { robot: "robot0", start: 0, duration: 1, warned: false };
    expect(isSupportTaskBar({ ...base, key: "open", label: "OpenFridge", op: "OpenFridge" })).toBe(true);
    expect(isSupportTaskBar({ ...base, key: "close", label: "CloseFridge", op: "CloseFridge" })).toBe(true);
    expect(isSupportTaskBar({ ...base, key: "rest", label: "navigate_rest", group: "robot0#go_to_rest" })).toBe(true);
    expect(isSupportTaskBar({ ...base, key: "yield", label: "navigate", group: "move#yield" })).toBe(false);
    expect(isSupportTaskBar({ ...base, key: "away", label: "navigate", group: "move#go_away_robot1" })).toBe(false);
    expect(isSupportTaskBar({ ...base, key: "detour", label: "navigate", group: "move#detour_robot1" })).toBe(false);
    expect(isSupportTaskBar({ ...base, key: "move", label: "place_mug", op: "place" })).toBe(false);
  });
});

describe("mergeResetBars", () => {
  it("folds contiguous resets into the preceding robot bar", () => {
    const result = mergeResetBars([
      bar({ key: "place", robot: "robot0", op: "place", start: 5, duration: 2 }),
      bar({ key: "reset-1", robot: "robot0", op: "reset", start: 7, duration: 0.5 }),
      bar({ key: "reset-2", robot: "robot0", op: "reset", start: 7.5, duration: 0.25 }),
      bar({ key: "next", robot: "robot0", op: "navigate", start: 7.75, duration: 3 }),
    ]);

    expect(result.map((item) => item.key)).toEqual(["place", "next"]);
    expect(result[0]).toMatchObject({
      label: "place",
      start: 5,
      duration: 2.75,
      memberKeys: ["place", "reset-1", "reset-2"],
      endStepKey: "reset-2",
    });
  });

  it("propagates reset warning and estimate state to the merged bar", () => {
    const result = mergeResetBars([
      bar({ key: "pick", robot: "robot0", op: "pick", start: 0, duration: 1 }),
      bar({
        key: "reset", robot: "robot0", op: "RESET", start: 1, duration: 1,
        warned: true, estimated: true,
      }),
    ]);

    expect(result[0].warned).toBe(true);
    expect(result[0].estimated).toBe(true);
  });

  it("keeps a reset separate without a contiguous predecessor on its robot", () => {
    const result = mergeResetBars([
      bar({ key: "other", robot: "robot1", op: "place", start: 0, duration: 2 }),
      bar({ key: "reset", robot: "robot0", op: "reset", start: 3, duration: 1 }),
      bar({ key: "place", robot: "robot0", op: "place", start: 5, duration: 1 }),
      bar({ key: "late-reset", robot: "robot0", op: "reset", start: 7, duration: 1 }),
    ]);

    expect(result.map((item) => item.key)).toEqual([
      "other", "reset", "place", "late-reset",
    ]);
  });
});
