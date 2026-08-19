r"""One-time migration: stamp recording metadata into legacy skill tracks.

Old tracks (extracted before recording metadata existed) carry no record of the
robot mount their base channels are relative to. For layout042 the recording
scene revision no longer exists on disk — its mounts were only preserved in the
now-retired ``skill_generators._SKILL_RECORD_MOUNT`` table. This migration
writes those values INTO each track's ``meta.recording`` explicitly, with
provenance, so the consumer never consults a global table again.

The migration refuses to run without explicit per-robot mounts: guessing (e.g.
reading the CURRENT scene's mounts) would silently re-anchor every replay to
the wrong pose, which is exactly the class of bug this metadata exists to
prevent. Tracks that already have ``meta.recording`` are left untouched.

Entry/exit world poses are computed against the given RECORD mount: the scene
model is loaded and the robot's mount body is transplanted to the record pose
before evaluating the track's first/last frame.

Usage (layout042 legacy values):
    uv run --with mujoco==3.10.0 python -m mujoco_skills.pipeline.migrate_track_mounts \
      --scene frontend/public/assets/robocasa/layout042_study.xml \
      --tracks-dir frontend/public/trajectories/layout042_study/tracks \
      --mount robot0:4.25723264892,-3.48884336639,0.000301804927632 \
      --mount robot1:1.158656,-6.210198,1.5707963267948966 \
      --reason "migrated from skill_generators._SKILL_RECORD_MOUNT (recording scene revision predates in-track metadata)"
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import mujoco

from mujoco_skills.model_signature import model_signature


def _parse_mount(spec: str) -> tuple[str, dict]:
    robot, values = spec.split(":", 1)
    x, y, yaw = (float(v) for v in values.split(","))
    return robot, {
        "position": [x, y, 0.0],
        "quaternion": [math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)],
        "yaw": yaw,
    }


def _base_world_pose(model, data, q, robot_idx: int) -> dict:
    data.qpos[:] = q
    mujoco.mj_forward(model, data)
    bid = mujoco.mj_name2id(
        model, mujoco.mjtObj.mjOBJ_BODY, f"mobilebase{robot_idx}_base")
    xy = data.xpos[bid][:2]
    rot = data.xmat[bid].reshape(3, 3)
    return {
        "xy": [round(float(xy[0]), 6), round(float(xy[1]), 6)],
        "yaw": round(float(np.arctan2(rot[1, 0], rot[0, 0])), 6),
    }


def migrate_track(track: dict, mount: dict, scene_id: str, reason: str,
                  model, data, name_to_adr) -> bool:
    """Stamp recording metadata + entry/exit poses. Returns False if the track
    already carries recording metadata (left untouched)."""
    meta = track["meta"]
    if meta.get("recording"):
        return False
    robot_idx = int(meta["robot_index"])

    meta["recording"] = {
        "scene_id": scene_id,
        # The recording scene revision is gone; its full structural signature
        # cannot be reconstructed. Consumers accept a null signature for
        # explicitly-migrated tracks (and only for those).
        "model_signature": None,
        "robot": f"robot{robot_idx}",
        "coordinate_frame": "robot_mount_local",
        "robot_mount": mount,
        "migration": {"source": "explicit --mount argument", "reason": reason},
    }

    for key, frame in (("entry_base_pose", 0), ("exit_base_pose", -1)):
        q = model.qpos0.copy()
        for jn, vals in track["channels"].items():
            adr = name_to_adr.get(jn)
            if adr is not None:
                q[adr:adr + len(vals[frame])] = vals[frame]
        meta[key] = _base_world_pose(model, data, q, robot_idx)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--tracks-dir", type=Path, required=True)
    parser.add_argument("--mount", type=_parse_mount, action="append", required=True,
                        metavar="robotN:x,y,yaw")
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    mounts = dict(args.mount)

    scene_id = args.scene.stem

    # transplant the record mounts onto the model so entry/exit poses are
    # evaluated in the recording frame, not the current one
    models: dict[str, tuple] = {}
    for robot, mount in mounts.items():
        m = mujoco.MjModel.from_xml_path(str(args.scene))
        bid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, f"{robot}_base")
        if bid < 0:
            raise SystemExit(f"scene has no body '{robot}_base'")
        m.body_pos[bid] = mount["position"]
        m.body_quat[bid] = mount["quaternion"]
        d = mujoco.MjData(m)
        mujoco.mj_forward(m, d)
        name_to_adr = {}
        for j in range(m.njnt):
            name_to_adr[mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_JOINT, j)] = int(m.jnt_qposadr[j])
        models[robot] = (m, d, name_to_adr)

    migrated = skipped = 0
    for robot_dir in sorted(p for p in args.tracks_dir.iterdir() if p.is_dir()):
        robot = robot_dir.name
        if robot not in mounts:
            continue
        m, d, name_to_adr = models[robot]
        for path in sorted(robot_dir.glob("*.track.json")):
            track = json.loads(path.read_text(encoding="utf-8"))
            if migrate_track(track, mounts[robot], scene_id, args.reason, m, d, name_to_adr):
                path.write_text(json.dumps(track, indent=1), encoding="utf-8")
                meta = track["meta"]
                print(f"[migrate] {robot}/{meta['skill']}: mount={mounts[robot]['position'][:2]} "
                      f"entry={meta['entry_base_pose']['xy']}")
                migrated += 1
            else:
                print(f"[skip] {robot}/{track['meta']['skill']}: already has recording metadata")
                skipped += 1
    print(f"migrated {migrated}, skipped {skipped}")


if __name__ == "__main__":
    main()
