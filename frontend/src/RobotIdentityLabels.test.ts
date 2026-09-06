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

  it("collects every namespaced body for legacy robot descriptors", () => {
    expect(
      discoverRobotIdentityAnchors(
        [
          { id: 0, name: "world", parentId: 0 },
          { id: 7, name: "robot0_base", parentId: 0 },
          { id: 8, name: "robot0_link0", parentId: 7 },
          { id: 9, name: "robot0_link1", parentId: 8 },
        ],
        {
          robot0: {
            index: 0,
          },
        },
      ),
    ).toEqual([
      { robot: "robot0", rootBodyId: 7, trackingBodyId: 8, bodyIds: [7, 8, 9] },
    ]);
  });

  it("maps a morphology-specific namespace back to its logical robot id", () => {
    expect(
      discoverRobotIdentityAnchors(
        [
          { id: 0, name: "world", parentId: 0 },
          { id: 20, name: "stretch1_base_link", parentId: 0 },
          { id: 21, name: "stretch1_link_lift", parentId: 20 },
          { id: 7, name: "robot0_base", parentId: 0 },
          { id: 8, name: "robot0_link0", parentId: 7 },
        ],
        {
          robot0: {
            index: 0,
            namespace: "robot0",
            root_body: "robot0_base",
            tracking_body: "robot0_link0",
          },
          robot1: {
            index: 1,
            namespace: "stretch1",
            root_body: "stretch1_base_link",
            tracking_body: "stretch1_base_link",
          },
        },
      ),
    ).toEqual([
      { robot: "robot0", rootBodyId: 7, trackingBodyId: 8, bodyIds: [7, 8] },
      { robot: "robot1", rootBodyId: 20, trackingBodyId: 20, bodyIds: [20, 21] },
    ]);
  });
});
