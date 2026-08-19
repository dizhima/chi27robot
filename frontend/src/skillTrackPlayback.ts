import type { MujocoSimAPI } from "mujoco-react";

/**
 * A single skill's channel track: a name-addressed, per-channel qpos timeline.
 * Only the executing robot's joints + the fixture joints it manipulates are
 * present, so applying it leaves every other channel (the other robot, a
 * cabinet left open by the init keyframe, untouched objects) as-is. This is
 * what makes multiple tracks compose onto one live state.
 */
export type SkillTrack = {
  meta: {
    skill: string;
    robot_index: number;
    scene_nq: number;
    fixture_joints: string[];
    n_frames: number;
    duration: number;
  };
  time: number[];
  phase: (string | null)[];
  channels: Record<string, number[][]>;
};

export type ChannelAddrMap = Map<string, number>;

/** Resolve a track's channel joint names to qpos addresses in the loaded scene. */
export function buildChannelAddrMap(
  api: MujocoSimAPI,
  channelNames: string[],
): ChannelAddrMap {
  const byName = new Map(api.getJoints().map((j) => [j.name, j.qposAdr]));
  const map: ChannelAddrMap = new Map();
  for (const name of channelNames) {
    const adr = byName.get(name);
    if (adr !== undefined) map.set(name, adr);
  }
  return map;
}

/**
 * Linear-interpolate `track` at wall-clock time `t` and overlay only its
 * channels onto `q` (mutated in place). `t` is clamped by the caller; times
 * past the end simply resolve to the last frame, so a finished skill's effect
 * (a closed door, the arm's end pose) persists when held.
 */
export function overlayTrackAtTime(
  q: Float64Array,
  track: SkillTrack,
  addrMap: ChannelAddrMap,
  t: number,
): void {
  const time = track.time;
  const last = time.length - 1;
  let i = 0;
  while (i < last && time[i + 1] <= t) i++;
  const j = Math.min(i + 1, last);
  const t0 = time[i];
  const t1 = time[j];
  const f = t1 > t0 ? (t - t0) / (t1 - t0) : 0;
  for (const [name, adr] of addrMap) {
    const a = track.channels[name][i];
    const b = track.channels[name][j];
    for (let k = 0; k < a.length; k++) {
      q[adr + k] = a[k] + (b[k] - a[k]) * f;
    }
  }
}

/** Phase label active at time `t` (nearest frame). */
export function phaseAtTime(track: SkillTrack, t: number): string | null {
  const time = track.time;
  const last = time.length - 1;
  let i = 0;
  while (i < last && time[i + 1] <= t) i++;
  const j = Math.min(i + 1, last);
  const f = time[j] > time[i] ? (t - time[i]) / (time[j] - time[i]) : 0;
  return track.phase[f < 0.5 ? i : j];
}
