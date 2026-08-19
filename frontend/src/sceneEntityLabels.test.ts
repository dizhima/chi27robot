import { describe, expect, it } from "vitest";
import type { SceneManifest } from "./authoring/types";
import { discoverSceneEntityLabelAnchors } from "./sceneEntityLabelModel";

const manifest: SceneManifest = {
  objects: {
    mug_1: { label: "mug", body: "mug_1_main" },
    apple_2: { label: "apple", body: 8, show_scene_label: false },
    missing: { body: "not_in_this_scene" },
  },
  facilities: {
    fridge: {
      label: "refrigerator",
      body: "fridge_main",
      articulation: { joints: ["door"] },
      scene_label_xy: [2.75, -1.1],
      standoff: { standoff_xy: [2.5, -1.25] },
    },
    upper_cabinet: {
      label: "upper cabinet",
      body: "cabinet_main",
      articulation: { joints: ["hinge"] },
      world_pos: [4, -2, 1.8],
    },
    drawer: {
      label: "drawer",
      articulation: { joints: ["slide"] },
      standoff: { standoff_xy: [3, -3] },
      show_scene_label: false,
    },
    sink: {
      label: "sink",
      body: "sink_main",
      scene_label_xy: [1.5, 0.75],
      scene_label_z: 1.0,
      world_pos: [1, 1, 1],
    },
  },
};

const bodies = [
  { id: 0, name: "world" },
  { id: 7, name: "mug_1_main" },
  { id: 8, name: "apple_2_main" },
];

describe("discoverSceneEntityLabelAnchors", () => {
  it("tracks visible objects and puts articulated facilities at their standoffs", () => {
    expect(discoverSceneEntityLabelAnchors(manifest, bodies, true)).toEqual([
      { kind: "object", name: "mug_1", text: "mug 1", bodyId: 7 },
      {
        kind: "facility",
        name: "fridge",
        text: "refrigerator",
        position: [2.75, -1.1, 2.0],
      },
      {
        kind: "facility",
        name: "upper_cabinet",
        text: "upper cabinet",
        position: [4, -2, 2.0],
      },
      {
        kind: "facility",
        name: "sink",
        text: "sink",
        position: [1.5, 0.75, 1.0],
      },
    ]);
  });

  it("lets the environment switch suppress objects without suppressing facilities", () => {
    expect(discoverSceneEntityLabelAnchors(manifest, bodies, false).map((anchor) => anchor.kind))
      .toEqual(["facility", "facility", "facility"]);
  });
});
