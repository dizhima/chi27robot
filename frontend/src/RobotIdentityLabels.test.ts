import { describe, expect, it } from "vitest";
import { discoverRobotIdentityAnchors } from "./robotIdentities";

describe("discoverRobotIdentityAnchors", () => {
  it("discovers and orders robots across scene-specific body ids", () => {
    expect(
      discoverRobotIdentityAnchors([
        { id: 0, name: "world", parentId: 0 },
        { id: 42, name: "robot1_base", parentId: 0 },
        { id: 43, name: "robot1_link0", parentId: 42 },
        { id: 7, name: "robot0_base", parentId: 0 },
        { id: 8, name: "robot0_link0", parentId: 7 },
      ]),
    ).toEqual([
      { robot: "robot0", rootBodyId: 7, trackingBodyId: 8, bodyIds: [7, 8] },
      { robot: "robot1", rootBodyId: 42, trackingBodyId: 43, bodyIds: [42, 43] },
    ]);
  });

  it("falls back to the shallowest namespaced body when `_base` is absent", () => {
    expect(
      discoverRobotIdentityAnchors([
        { id: 0, name: "world", parentId: 0 },
        { id: 5, name: "mount", parentId: 0 },
        { id: 11, name: "robot0_chassis", parentId: 5 },
        { id: 12, name: "robot0_arm", parentId: 11 },
      ]),
    ).toEqual([{
      robot: "robot0",
      rootBodyId: 11,
      trackingBodyId: 11,
      bodyIds: [11, 12],
    }]);
  });
});
