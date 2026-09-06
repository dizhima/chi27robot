import { describe, expect, it } from "vitest";
import {
  DEFAULT_SCENE_LABEL_SCALE,
  resolveSceneLabelScale,
} from "./sceneLabelScale";

describe("resolveSceneLabelScale", () => {
  it("accepts a positive scale", () => {
    expect(resolveSceneLabelScale("0.8")).toBe(0.8);
    expect(resolveSceneLabelScale(1.25)).toBe(1.25);
  });

  it.each([undefined, "", "invalid", "0", "-1", Number.POSITIVE_INFINITY])(
    "falls back to the default for %s",
    (value) => {
      expect(resolveSceneLabelScale(value)).toBe(DEFAULT_SCENE_LABEL_SCALE);
    },
  );
});
