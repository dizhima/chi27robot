import { describe, expect, it } from "vitest";
import {
  assignPinHandles,
  buildBodyIndex,
  refFromPick,
  refLabel,
  refText,
  resolveBody,
  serializeRefs,
  type SceneContextRef,
} from "./sceneContext";
import type { SceneManifest } from "../authoring/types";

const MANIFEST: SceneManifest = {
  objects: {
    mug_1: { body: "mug_1_main", label: "mug" },
    apple_2: { body: "apple_2_main", label: "apple" },
  },
  facilities: {
    sink: {
      body: "sink_island_group_1_main",
      label: "sink",
      place: { kind: "surface", surface_body: "sink_island_group_1_main" },
    },
    counter_left: {
      body: "counter_1_left_group_1_main",
      label: "left counter",
      place: { kind: "surface", surface_body: "counter_1_left_group_1_main" },
    },
  },
};

// bodyId -> {name, parentId}. 1 is a leaf geom body under the counter group (7).
const BODY_META = new Map<number, { name: string; parentId: number }>([
  [0, { name: "world", parentId: 0 }],
  [3, { name: "mug_1_main", parentId: 0 }],
  [7, { name: "counter_1_left_group_1_main", parentId: 0 }],
  [1, { name: "counter_1_left_leaf", parentId: 7 }],
  [9, { name: "floor", parentId: 0 }],
]);

describe("buildBodyIndex", () => {
  it("maps object + facility + surface bodies back to their names", () => {
    const index = buildBodyIndex(MANIFEST);
    expect(index.get("mug_1_main")).toEqual({ kind: "object", name: "mug_1" });
    expect(index.get("counter_1_left_group_1_main")).toEqual({
      kind: "facility",
      name: "counter_left",
    });
    expect(index.get("sink_island_group_1_main")).toEqual({ kind: "facility", name: "sink" });
  });
});

describe("resolveBody", () => {
  const index = buildBodyIndex(MANIFEST);

  it("resolves a direct object body hit", () => {
    expect(resolveBody(3, BODY_META, index)).toEqual({
      attribution: { kind: "object", name: "mug_1" },
      bodyName: "mug_1_main",
    });
  });

  it("walks up to the facility group when a leaf geom body is hit", () => {
    expect(resolveBody(1, BODY_META, index)).toEqual({
      attribution: { kind: "facility", name: "counter_left" },
      bodyName: "counter_1_left_leaf",
    });
  });

  it("returns null attribution for an unmapped body (floor)", () => {
    expect(resolveBody(9, BODY_META, index)).toEqual({ attribution: null, bodyName: "floor" });
  });
});

describe("refFromPick", () => {
  it("makes an object ref (no marker) for an object hit", () => {
    const ref = refFromPick("id1", 3, "mug_1_main", [1, 2, 3], { kind: "object", name: "mug_1" });
    expect(ref).toMatchObject({ kind: "object", name: "mug_1", body: "mug_1_main", worldPos: [1, 2, 3] });
  });

  it("makes a position ref with onFacility for a surface hit", () => {
    const ref = refFromPick("id2", 7, "counter_1_left_group_1_main", [4, 5, 6], {
      kind: "facility",
      name: "counter_left",
    });
    expect(ref).toMatchObject({ kind: "position", xyz: [4, 5, 6], onFacility: "counter_left", onObject: null });
  });

  it("makes a position ref with null attribution for a floor hit", () => {
    const ref = refFromPick("id3", 9, "floor", [7, 8, 0], null);
    expect(ref).toMatchObject({ kind: "position", onFacility: null, onObject: null, bodyName: "floor" });
  });
});

describe("labels + serialization", () => {
  const objectRef: SceneContextRef = {
    kind: "object",
    id: "a",
    name: "mug_1",
    body: "mug_1_main",
    bodyId: 3,
    worldPos: [1, 2, 3],
  };
  const facilityPin: SceneContextRef = {
    kind: "position",
    id: "b",
    xyz: [4, 5, 6],
    onFacility: "counter_left",
    onObject: null,
    bodyId: 7,
    bodyName: "counter_1_left_group_1_main",
  };
  const floorPin: SceneContextRef = {
    kind: "position",
    id: "c",
    xyz: [7, 8, 0],
    onFacility: null,
    onObject: null,
    bodyId: 9,
    bodyName: "floor",
  };

  it("labels + text align to manifest names, with a fallback for free points", () => {
    expect(refLabel(objectRef)).toBe("mug_1");
    expect(refText(objectRef)).toBe("mug_1");
    expect(refLabel(facilityPin)).toBe("counter_left");
    expect(refText(facilityPin)).toBe("counter_left");
    expect(refText(floorPin)).toBe("the marked location");
  });

  it("serializes to snake_case author-aligned shapes", () => {
    expect(serializeRefs([objectRef, facilityPin, floorPin])).toEqual([
      { kind: "object", name: "mug_1", body: "mug_1_main", body_id: 3, world_pos: [1, 2, 3] },
      {
        kind: "position",
        xyz: [4, 5, 6],
        on_facility: "counter_left",
        on_object: null,
        body_id: 7,
        body_name: "counter_1_left_group_1_main",
      },
      {
        kind: "position",
        xyz: [7, 8, 0],
        on_facility: null,
        on_object: null,
        body_id: 9,
        body_name: "floor",
      },
    ]);
  });

  describe("assignPinHandles", () => {
    const sinkPin: SceneContextRef = {
      kind: "position",
      id: "d",
      xyz: [1, 1, 1],
      onFacility: "sink",
      onObject: null,
      bodyId: 10,
      bodyName: "sink_island_group_1_main",
    };

    it("assigns p1/p2 in document order to bindable positions only, skipping objects and null-facility pins", () => {
      const handles = assignPinHandles([objectRef, facilityPin, floorPin, sinkPin]);
      expect(handles.get(facilityPin.id)).toBe("p1");
      expect(handles.get(sinkPin.id)).toBe("p2");
      expect(handles.has(objectRef.id)).toBe(false);
      expect(handles.has(floorPin.id)).toBe(false);
      expect(handles.size).toBe(2);
    });

    it("threads into serializeRefs: handle present for bindable pins, absent for objects and null-facility pins", () => {
      const handles = assignPinHandles([facilityPin, floorPin, sinkPin]);
      const serialized = serializeRefs([objectRef, facilityPin, floorPin, sinkPin], handles) as Record<
        string,
        unknown
      >[];
      expect(serialized[0].handle).toBeUndefined(); // object
      expect(serialized[1].handle).toBe("p1"); // facilityPin
      expect(serialized[2].handle).toBeUndefined(); // floorPin
      expect(serialized[3].handle).toBe("p2"); // sinkPin
    });

    it("omits handle fields entirely when serializeRefs is called without a handles map (back-compat)", () => {
      const serialized = serializeRefs([facilityPin]) as Record<string, unknown>[];
      expect("handle" in serialized[0]).toBe(false);
    });
  });
});
