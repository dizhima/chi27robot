import { describe, expect, it } from "vitest";
import { clusterConflicts } from "./ganttConflicts";
import type { Conflict } from "./planTypes";

const mk = (message: string, window: [number, number] | null): Conflict => ({
  kind: "path",
  steps: [],
  robots: [],
  window,
  detail: {},
  message,
});

describe("clusterConflicts", () => {
  it("returns no clusters for empty input", () => {
    expect(clusterConflicts([], 100)).toEqual([]);
  });

  it("returns a single cluster for a single conflict", () => {
    const clusters = clusterConflicts([mk("a", [10, 20])], 100);
    expect(clusters).toHaveLength(1);
    expect(clusters[0].mid).toBe(15);
    expect(clusters[0].start).toBe(10);
    expect(clusters[0].end).toBe(20);
    expect(clusters[0].conflicts).toHaveLength(1);
  });

  it("merges two overlapping windows into one cluster", () => {
    const clusters = clusterConflicts([mk("a", [10, 20]), mk("b", [15, 25])], 100);
    expect(clusters).toHaveLength(1);
    expect(clusters[0].conflicts).toHaveLength(2);
    expect(clusters[0].start).toBe(10);
    expect(clusters[0].end).toBe(25);
  });

  it("keeps two far-apart windows as separate clusters", () => {
    const clusters = clusterConflicts([mk("a", [0, 5]), mk("b", [90, 95])], 100);
    expect(clusters).toHaveLength(2);
    expect(clusters[0].conflicts).toHaveLength(1);
    expect(clusters[1].conflicts).toHaveLength(1);
  });

  it("excludes null-window conflicts", () => {
    const clusters = clusterConflicts([mk("a", null), mk("b", [10, 20])], 100);
    expect(clusters).toHaveLength(1);
    expect(clusters[0].conflicts.map((c) => c.message)).toEqual(["b"]);
  });
});
