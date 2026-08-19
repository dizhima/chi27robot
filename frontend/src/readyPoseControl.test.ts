import { describe, expect, it } from "vitest";
import type { ActuatedJointInfo } from "mujoco-react";
import { discoverReadyPoseJoints, readyPoseTorque } from "./readyPoseControl";

function arm(robot: number, joint: number, overrides: Partial<ActuatedJointInfo> = {}): ActuatedJointInfo {
  return {
    id: joint,
    name: `robot${robot}_joint${joint}`,
    type: 3,
    typeName: "hinge",
    range: [-3, 3],
    limited: true,
    bodyId: 1,
    qposAdr: robot * 10 + joint,
    dofAdr: robot * 10 + joint,
    actuatorId: robot * 10 + joint,
    actuatorName: `robot${robot}_torq_j${joint}`,
    ctrlAdr: robot * 10 + joint,
    ctrlRange: [-100, 100],
    ...overrides,
  };
}

describe("ready pose arm discovery", () => {
  it("discovers complete robots in numeric order even with shuffled actuator info", () => {
    const entries = [
      ...Array.from({ length: 7 }, (_, index) => arm(10, index + 1)),
      ...Array.from({ length: 7 }, (_, index) => arm(2, index + 1)),
    ].reverse();
    const result = discoverReadyPoseJoints(entries);
    expect(result.warnings).toEqual([]);
    expect(result.joints.map((joint) => `${joint.robot}:${joint.jointIndex}`)).toEqual([
      "robot2:1", "robot2:2", "robot2:3", "robot2:4", "robot2:5", "robot2:6", "robot2:7",
      "robot10:1", "robot10:2", "robot10:3", "robot10:4", "robot10:5", "robot10:6", "robot10:7",
    ]);
  });

  it("ignores unrelated joints and skips incomplete, duplicate, mismatched, and invalid robots", () => {
    const valid = Array.from({ length: 7 }, (_, index) => arm(0, index + 1));
    const incomplete = Array.from({ length: 6 }, (_, index) => arm(1, index + 1));
    const duplicate = [...Array.from({ length: 7 }, (_, index) => arm(2, index + 1)), arm(2, 1)];
    const mismatched = Array.from({ length: 7 }, (_, index) => arm(3, index + 1, {
      actuatorName: `robot4_torq_j${index + 1}`,
    }));
    const invalid = Array.from({ length: 7 }, (_, index) => arm(5, index + 1, {
      ctrlAdr: index === 0 ? -1 : index,
    }));
    const unrelated = arm(9, 1, { name: "robot9_base_x", actuatorName: "robot9_base_x" });
    const result = discoverReadyPoseJoints([...valid, ...incomplete, ...duplicate, ...mismatched, ...invalid, unrelated]);
    expect(result.joints).toHaveLength(7);
    expect(result.joints.every((joint) => joint.robot === "robot0")).toBe(true);
    expect(result.warnings).toHaveLength(4);
  });
});

describe("readyPoseTorque", () => {
  const joint = { jointIndex: 1, ctrlRange: [-10, 10] as const };

  it("adds bias and PD correction, then clamps at both actuator limits", () => {
    expect(readyPoseTorque(joint, 1, 1, 0, 3)).toBe(3);
    expect(readyPoseTorque(joint, 1, 0, 0, 0)).toBe(10);
    expect(readyPoseTorque(joint, 0, 1, 0, 0)).toBe(-10);
    expect(readyPoseTorque(joint, 0, 0, 1, 0)).toBe(-10);
  });

  it("fails closed for invalid numeric input", () => {
    expect(readyPoseTorque(joint, Number.NaN, 0, 0, 0)).toBe(0);
    expect(readyPoseTorque(joint, 0, 0, Number.POSITIVE_INFINITY, 0)).toBe(0);
    expect(readyPoseTorque({ jointIndex: 1, ctrlRange: [2, 1] }, 0, 0, 0, 0)).toBe(0);
  });
});
