import { describe, expect, it } from "vitest";
import {
  canEnterExplore,
  canUsePlanPlayback,
  shouldEnableExploreTools,
  shouldPauseScene,
} from "./exploreMode";

describe("explore mode predicates", () => {
  it("permits a draft plan to enter Explore before it is compiled", () => {
    expect(canEnterExplore({ hasPlan: true, simReady: true })).toBe(true);
    expect(canEnterExplore({ hasPlan: false, simReady: true })).toBe(false);
    expect(canEnterExplore({ hasPlan: true, simReady: false })).toBe(false);
  });

  it("permits a spatial draft to replay synchronized compiled output, but blocks structural drafts", () => {
    expect(canUsePlanPlayback({ hasPlan: true, inSync: true, dirty: false, itemCount: 2, status: "ready" })).toBe(true);
    expect(canUsePlanPlayback({ hasPlan: true, inSync: true, dirty: true, editImpact: "spatial", itemCount: 2, status: "ready" })).toBe(true);
    expect(canUsePlanPlayback({ hasPlan: true, inSync: true, dirty: true, editImpact: "structural", itemCount: 2, status: "ready" })).toBe(false);
    expect(canUsePlanPlayback({ hasPlan: true, inSync: true, dirty: false, itemCount: 0, status: "ready" })).toBe(false);
    expect(canUsePlanPlayback({ hasPlan: true, inSync: true, dirty: false, itemCount: 2, status: "compiling" })).toBe(false);
  });

  it("unpauses and enables direct manipulation only in no-plan or Explore mode", () => {
    expect(shouldPauseScene({ hasPlan: true, exploreMode: false, simReady: true })).toBe(true);
    expect(shouldPauseScene({ hasPlan: true, exploreMode: true, simReady: true })).toBe(false);
    expect(shouldPauseScene({ hasPlan: false, exploreMode: false, simReady: true })).toBe(false);
    expect(shouldPauseScene({ hasPlan: false, exploreMode: false, simReady: false })).toBe(true);
    expect(shouldEnableExploreTools(true, false)).toBe(false);
    expect(shouldEnableExploreTools(true, true)).toBe(true);
    expect(shouldEnableExploreTools(false, false)).toBe(true);
  });
});
