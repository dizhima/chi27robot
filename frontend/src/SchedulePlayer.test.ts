import { describe, expect, it } from "vitest";
import {
  selectObjectController,
  type ScheduledItem,
} from "./SchedulePlayer";
import type { SkillTrack } from "./skillTrackPlayback";

function item(
  key: string,
  start: number,
  duration: number,
  object: string | null,
  channels: string[],
): ScheduledItem {
  const track: SkillTrack = {
    meta: {
      skill: key,
      robot_index: key === "owner" ? 0 : 1,
      scene_nq: 10,
      fixture_joints: [],
      n_frames: 2,
      duration,
    },
    time: [0, duration],
    phase: [null, null],
    channels: Object.fromEntries(
      channels.map((channel) => [channel, [[0], [1]]]),
    ),
  };
  return { key, robot: "robot0", skill: key, track, start, duration, object };
}

describe("selectObjectController", () => {
  it("keeps an actively carried object with its owner over a later secondary track", () => {
    const owner = item("owner", 40, 12, "apple_1", ["apple_1_joint0"]);
    const secondary = item("secondary", 50, 9, "apple_2", [
      "apple_1_joint0",
      "apple_2_joint0",
    ]);

    expect(selectObjectController([owner, secondary], "apple_1", 50.5)).toBe(owner);
  });

  it("allows a later world-effect track after primary ownership is inactive", () => {
    const owner = item("owner", 40, 10, "apple_1", ["apple_1_joint0"]);
    const secondary = item("secondary", 50, 9, "apple_2", ["apple_1_joint0"]);

    expect(selectObjectController([owner, secondary], "apple_1", 50.5)).toBe(secondary);
  });
});
