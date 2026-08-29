import { describe, expect, it } from "vitest";
import { robotIdsFromManifest } from "./robotRegistry";
import type { SceneManifest } from "./authoring/types";

describe("robotIdsFromManifest", () => {
  it("uses manifest indices to produce stable dynamic lanes", () => {
    const manifest = {
      robots: {
        robot2: { index: 2 },
        robot0: { index: 0 },
        robot1: { index: 1 },
      },
      objects: {},
      facilities: {},
    } satisfies SceneManifest;

    expect(robotIdsFromManifest(manifest)).toEqual(["robot0", "robot1", "robot2"]);
  });

  it("does not invent robots before a manifest is available", () => {
    expect(robotIdsFromManifest(null)).toEqual([]);
  });
});
