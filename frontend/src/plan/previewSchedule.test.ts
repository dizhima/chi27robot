import { describe, expect, it } from "vitest";
import { previewBars, type PreviewMeta } from "./previewSchedule";
import type { AuthoredPlan } from "./planTypes";

const meta = (dur: number, label: string): PreviewMeta => ({ duration: dur, label });

// robot0: a(10) -> b(5) ; robot1: c(6) -> d(4)
const metaById = new Map<string, PreviewMeta>([
  ["a", meta(10, "nav_a")],
  ["b", meta(5, "place_a")],
  ["c", meta(6, "nav_c")],
  ["d", meta(4, "place_c")],
]);

const plan = (): AuthoredPlan => ({
  tasks: [
    { robot: "robot0", steps: [{ id: "a", op: "navigate" }, { id: "b", op: "place" }] },
    { robot: "robot1", steps: [{ id: "c", op: "navigate" }, { id: "d", op: "place" }] },
  ],
});

const startOf = (bars: ReturnType<typeof previewBars>, id: string) =>
  bars.find((b) => b.key === id)?.start;

describe("previewBars", () => {
  it("packs each robot's steps back-to-back from 0", () => {
    const bars = previewBars(plan(), metaById);
    expect(startOf(bars, "a")).toBe(0);
    expect(startOf(bars, "b")).toBe(10); // after a (dur 10)
    expect(startOf(bars, "c")).toBe(0);
    expect(startOf(bars, "d")).toBe(6);
  });

  it("delays a step (and its lane successors) to an after-dep's end", () => {
    // robot1's c waits for robot0's b (ends at 15) -> c=15, d=21
    const p = plan();
    p.tasks[1].steps[0].after = ["b"];
    const bars = previewBars(p, metaById);
    expect(startOf(bars, "c")).toBe(15);
    expect(startOf(bars, "d")).toBe(21); // c starts 15, dur 6 -> ends 21
  });

  it("takes the max of sequential and dependency constraints", () => {
    // d additionally waits on a (ends 10); its sequential start is 6 -> max = 10
    const p = plan();
    p.tasks[1].steps[1].after = ["a"];
    const bars = previewBars(p, metaById);
    expect(startOf(bars, "d")).toBe(10);
  });

  it("carries label/duration/facility from meta", () => {
    const bars = previewBars(plan(), metaById);
    const a = bars.find((b) => b.key === "a");
    expect(a?.label).toBe("nav_a");
    expect(a?.duration).toBe(10);
    expect(a?.warned).toBe(false);
  });

  it("falls back to nominal width + authored-step label before compile", () => {
    // No compiled meta yet (never-compiled draft): even-width structural preview.
    const p: AuthoredPlan = {
      tasks: [
        {
          robot: "robot0",
          steps: [
            { id: "a", op: "navigate", target: "mug_1" },
            { id: "b", op: "pick", object: "mug_1" },
          ],
        },
      ],
    };
    const bars = previewBars(p, new Map());
    expect(bars.map((b) => [b.label, b.duration, b.start])).toEqual([
      ["navigate", 1, 0],
      ["pick", 1, 1], // packed after a's nominal width
    ]);
    expect(bars.every((b) => b.estimated)).toBe(true);
  });

  it("uses same-op compiled medians for newly added steps", () => {
    const p: AuthoredPlan = {
      tasks: [
        {
          robot: "robot0",
          steps: [
            { id: "a", op: "navigate" },
            { id: "b", op: "place" },
            { id: "new-nav", op: "navigate", target: "drawer_right" },
            { id: "new-place", op: "place", object: "mug_1", dest: "drawer_right" },
          ],
        },
      ],
    };

    const bars = previewBars(p, metaById);

    expect(bars.find((b) => b.key === "new-nav")?.duration).toBe(10);
    expect(bars.find((b) => b.key === "new-place")?.duration).toBe(5);
    expect(bars.find((b) => b.key === "new-nav")?.estimated).toBe(true);
  });

  it("uses a bounded compiled median for a previously unseen op", () => {
    const p: AuthoredPlan = {
      tasks: [
        {
          robot: "robot0",
          steps: [{ id: "new-open", op: "OpenDrawer" }],
        },
      ],
    };

    // Global median of 4, 5, 6, 10 is 5.5 seconds.
    const bars = previewBars(p, metaById);
    expect(bars[0].duration).toBe(5.5);
  });

  it("uses the authored wait duration instead of an estimate", () => {
    const p: AuthoredPlan = {
      tasks: [
        {
          robot: "robot0",
          steps: [{ id: "new-wait", op: "wait", duration: 18 }],
        },
      ],
    };

    const bars = previewBars(p, metaById);
    expect(bars[0].duration).toBe(18);
  });
});
