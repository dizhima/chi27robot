r"""(Re)bake the `study_init` keyframe for a study scene.

The keyframe is the scene's canonical initial session state, applied by the
frontend on load and reset to at the start of skill playback. It captures:

* a consistent arm "ready" pose for BOTH robots — arm(7) + torso + gripper taken
  from a reference demo's first frame (default robot0/OpenFridge), so the
  initial view and every skill's start pose agree. The ready pose excludes the
  mobile base, so it is reproducible at any base location;
* optionally, fixtures that must START OPEN (`--init-open robot0/CloseCabinet`):
  the named track's fixture joints are set to its frame-0 values. This is the
  workaround for a fixture that has a Close demo but no Open demo (used by
  layout042's upper cabinet).

Run (042 legacy behaviour):
    uv run --with mujoco==3.10.0 python -m mujoco_skills.pipeline.bake_study_init \
      --init-open robot0/CloseCabinet

Run (a scene whose fixtures all start closed):
    uv run --with mujoco==3.10.0 python -m mujoco_skills.pipeline.bake_study_init \
      --scene frontend/public/assets/robocasa/layout012_study.xml \
      --tracks-dir frontend/public/trajectories/layout012_study/tracks
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np
import mujoco

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCENE = ROOT / "frontend/public/assets/robocasa/layout042_study.xml"
DEFAULT_TRACKS = ROOT / "frontend/public/trajectories/layout042_study/tracks"

# joints that make up the arm "ready" pose (suffixes shared by both robots)
ARM_SUFFIXES = [f"joint{i}" for i in range(1, 8)]  # robot{n}_joint{i}
TORSO_SUFFIX = "joint_torso_height"                # mobilebase{n}_joint_torso_height
FINGER_SUFFIXES = ["right_finger_joint1", "right_finger_joint2"]  # gripper{n}_...


def qadr(model, name):
    return int(model.jnt_qposadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)])


def frame0(track_path):
    tr = json.loads(Path(track_path).read_text(encoding="utf-8"))
    return {name: vals[0] for name, vals in tr["channels"].items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, default=DEFAULT_SCENE)
    parser.add_argument("--tracks-dir", type=Path, default=DEFAULT_TRACKS)
    parser.add_argument(
        "--ready-from", default="robot0/OpenFridge",
        help="robot/SKILL track whose frame 0 supplies the arm ready pose",
    )
    parser.add_argument(
        "--init-open", action="append", default=[],
        help="robot/SKILL track whose frame-0 fixture-joint values are baked "
             "in (sets a fixture open when it lacks an Open demo); repeatable",
    )
    parser.add_argument(
        "--no-mjb", action="store_true",
        help="skip recompiling the sibling .mjb after baking the XML",
    )
    args = parser.parse_args()
    scene, tracks = args.scene, args.tracks_dir

    model = mujoco.MjModel.from_xml_path(str(scene))
    q = model.qpos0.copy()

    # --- fixtures that must start open (frame 0 of a Close demo) ----------
    for spec in args.init_open:
        robot, skill = spec.split("/", 1)
        tr = json.loads((tracks / robot / f"{skill}.track.json").read_text(encoding="utf-8"))
        for jn in tr["meta"]["fixture_joints"]:
            q[qadr(model, jn)] = tr["channels"][jn][0][0]
            print(f"init-open: {jn} = {tr['channels'][jn][0][0]:.3f} (from {spec} frame 0)")

    # --- ready arm pose (frame 0 of the reference demo), applied to BOTH --
    ready_robot, ready_skill = args.ready_from.split("/", 1)
    of = frame0(tracks / ready_robot / f"{ready_skill}.track.json")
    ready_idx = ready_robot.replace("robot", "")
    ready_arm = [of[f"robot{ready_idx}_{s}"][0] for s in ARM_SUFFIXES]
    ready_torso = of[f"mobilebase{ready_idx}_{TORSO_SUFFIX}"][0]
    # gripper fully open (range extremes) so ready always shows an open gripper,
    # matching the pick/place generators' open state.
    r1 = model.jnt_range[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "gripper0_right_finger_joint1")]
    r2 = model.jnt_range[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "gripper0_right_finger_joint2")]
    ready_fingers = [float(r1[1]), float(r2[0])]

    for r in (0, 1):
        for s, val in zip(ARM_SUFFIXES, ready_arm):
            q[qadr(model, f"robot{r}_{s}")] = val
        q[qadr(model, f"mobilebase{r}_{TORSO_SUFFIX}")] = ready_torso
        for s, val in zip(FINGER_SUFFIXES, ready_fingers):
            q[qadr(model, f"gripper{r}_{s}")] = val

    # --- rewrite the study_init keyframe qpos in the XML ------------------
    xml = scene.read_text(encoding="utf-8")
    qstr = " ".join(f"{v:.10g}" for v in q)
    new_key = f'<key name="study_init" qpos="{qstr}" />'
    if '<key name="study_init"' in xml:
        xml = re.sub(r'<key name="study_init"[^/]*/>', new_key, xml, count=1)
    elif "<keyframe />" in xml:
        xml = xml.replace("<keyframe />", f"<keyframe>{new_key}</keyframe>", 1)
    elif "<keyframe>" in xml:
        xml = xml.replace("<keyframe>", f"<keyframe>{new_key}", 1)
    else:
        xml = xml.replace("</mujoco>", f"<keyframe>{new_key}</keyframe></mujoco>", 1)
    scene.write_text(xml, encoding="utf-8")

    # --- verify by reloading -------------------------------------------------
    m2 = mujoco.MjModel.from_xml_path(str(scene))
    key = m2.key_qpos[0]
    print(f"baked study_init (nkey={m2.nkey}, name={mujoco.mj_id2name(m2, mujoco.mjtObj.mjOBJ_KEY, 0)})")
    print("ready arm (7):", [round(v, 4) for v in ready_arm])
    print("ready torso:", round(float(ready_torso), 4), " fingers:", [round(v, 4) for v in ready_fingers])
    for r in (0, 1):
        got = [float(key[qadr(m2, f"robot{r}_{s}")]) for s in ARM_SUFFIXES]
        match = np.allclose(got, ready_arm, atol=1e-5)
        print(f"  robot{r} arm in keyframe == ready: {match}  {[round(v, 4) for v in got]}")

    if not args.no_mjb:
        mjb = scene.with_suffix(".mjb")
        mujoco.mj_saveModel(m2, str(mjb))
        print(f"recompiled {mjb.name} ({mjb.stat().st_size/1e6:.0f}MB, nkey={m2.nkey})")


if __name__ == "__main__":
    main()
