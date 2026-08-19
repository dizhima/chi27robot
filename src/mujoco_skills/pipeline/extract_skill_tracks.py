r"""Convert raw RoboCasa replay dumps into assembly-ready *skill tracks*.

A raw dump from ``replay_atomic_on_scene`` is a list of frames, each carrying
the *full* ``qpos`` vector for the whole scene (both robots + every fixture and
object). Playing it back verbatim is wrong for multi-robot assembly:

* the robot that was *not* commanded sits frozen at its recorded pose, so it
  visually overlaps whatever that robot is doing in another skill's dump;
* channels are addressed by bare index, so any scene edit that shifts the qpos
  layout silently corrupts every dump.

A *skill track* fixes both. It keeps only the channels that belong to the
executing robot (all of them, so the start pose is pinned) plus the fixture
joints that actually moved, and addresses them **by joint name**. At playback
the frontend writes only those named channels onto the live state, leaving the
other robot and untouched objects exactly where they are, so tracks compose.

Usage (batch, both robots, all skills under a study dir):

    uv run --with mujoco==3.10.0 python -m mujoco_skills.pipeline.extract_skill_tracks \
      --scene   frontend/public/assets/robocasa/layout042_study.xml \
      --input-dir  frontend/public/trajectories/layout042_study \
      --output-dir frontend/public/trajectories/layout042_study/tracks

Coordinate-translation retarget (see docs/skill_track_retarget.md) synthesises a
skill on a second, geometrically identical fixture:

    ... --retarget robot0:OpenDrawer:stack_4_right_group_1_2_slidejoint:stack_2_right_group_1_3_slidejoint:OpenDrawer_stack2

Time-reverse synthesises the missing half of an open/close pair by playing an
extracted (or retargeted) track backwards — e.g. a layout with only CloseDrawer
demos gets OpenDrawer for free. Kinematically exact (same poses, opposite
order); phase labels are reversed verbatim, so treat them as cosmetic on
reversed tracks:

    ... --reverse robot0:CloseDrawer:OpenDrawer

Specs are applied after extraction, --retarget before --reverse, and each
synthesised track is registered under its new skill name, so chains like
retarget-then-reverse work in one invocation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

try:
    import mujoco
except ImportError as error:  # pragma: no cover - dependency hint
    raise SystemExit(
        "mujoco is required; run via: uv run --with mujoco==3.10.0 python ..."
    ) from error

from mujoco_skills.model_signature import model_signature, robot_mounts


ROBOT_PREFIXES = ("robot", "mobilebase", "gripper")
MOVE_THRESHOLD = 1e-4
# retarget geometric tolerances
QUAT_TOL = 1e-3
AXIS_TOL = 1e-3
RANGE_TOL = 1e-3
RESIDUAL_TOL = 1e-3  # world delta must lie in the base's horizontal plane


class JointMap:
    """Name <-> qpos-address bookkeeping for one compiled scene."""

    def __init__(self, scene_xml: Path):
        self.model = mujoco.MjModel.from_xml_path(str(scene_xml))
        self.data = mujoco.MjData(self.model)
        mujoco.mj_forward(self.model, self.data)
        self.nq = self.model.nq
        self.scene_id = Path(scene_xml).stem
        self.model_signature = model_signature(self.model)
        self.robot_mounts = robot_mounts(self.model)

        self.name_to_adr: dict[str, int] = {}
        self.name_to_size: dict[str, int] = {}
        self.adr_to_name: dict[int, str] = {}
        for j in range(self.model.njnt):
            name = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_JOINT, j)
            adr = int(self.model.jnt_qposadr[j])
            jtype = self.model.jnt_type[j]
            size = 7 if jtype == 0 else (4 if jtype == 1 else 1)
            self.name_to_adr[name] = adr
            self.name_to_size[name] = size
            for k in range(adr, adr + size):
                self.adr_to_name[k] = name

    def base_world_pose(self, q: np.ndarray, robot_idx: int) -> dict:
        """World (x, y, yaw) of the MOVING base body (mobilebase{N}_base) for
        qpos ``q`` — where the robot actually stands, not its fixed mount."""
        saved = self.data.qpos.copy()
        self.data.qpos[:] = q
        mujoco.mj_forward(self.model, self.data)
        bid = mujoco.mj_name2id(
            self.model, mujoco.mjtObj.mjOBJ_BODY, f"mobilebase{robot_idx}_base")
        xy = self.data.xpos[bid][:2]
        rot = self.data.xmat[bid].reshape(3, 3)
        pose = {
            "xy": [round(float(xy[0]), 6), round(float(xy[1]), 6)],
            "yaw": round(float(np.arctan2(rot[1, 0], rot[0, 0])), 6),
        }
        self.data.qpos[:] = saved
        mujoco.mj_forward(self.model, self.data)
        return pose

    def joint_world_axis(self, name: str) -> np.ndarray:
        jid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)
        return np.array(self.data.xaxis[jid], dtype=float)

    def joint_world_anchor(self, name: str) -> np.ndarray:
        jid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)
        return np.array(self.data.xanchor[jid], dtype=float)

    def joint_body_quat(self, name: str) -> np.ndarray:
        jid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)
        bid = int(self.model.jnt_bodyid[jid])
        return np.array(self.data.xquat[bid], dtype=float)

    def joint_range(self, name: str) -> np.ndarray:
        jid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)
        return np.array(self.model.jnt_range[jid], dtype=float)


def _load_raw(path: Path) -> tuple[np.ndarray, list[float], list]:
    frames = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(frames, list) or not frames:
        raise ValueError(f"raw dump is not a non-empty frame list: {path}")
    q = np.array([f["qpos"] for f in frames], dtype=float)
    time = [float(f.get("time", i)) for i, f in enumerate(frames)]
    phase = [f.get("phase") for f in frames]
    return q, time, phase


def _robot_index_of(joint_name: str) -> int | None:
    for prefix in ROBOT_PREFIXES:
        if joint_name.startswith(prefix):
            rest = joint_name[len(prefix):]
            digits = ""
            for ch in rest:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if digits:
                return int(digits)
    return None


def _detect_executing_robot(jmap: JointMap, moved_adrs: np.ndarray) -> int:
    """The robot index whose channels move in this dump."""
    indices = set()
    for adr in moved_adrs:
        name = jmap.adr_to_name.get(int(adr))
        if name is None:
            continue
        idx = _robot_index_of(name)
        if idx is not None:
            indices.add(idx)
    if len(indices) != 1:
        raise ValueError(
            f"could not uniquely detect the executing robot (found {sorted(indices)}); "
            "pass --robot to override"
        )
    return next(iter(indices))


def extract_track(
    jmap: JointMap,
    raw_path: Path,
    skill: str,
    robot_override: int | None = None,
) -> dict:
    """Build a skill track from one raw dump."""
    q, time, phase = _load_raw(raw_path)
    if q.shape[1] != jmap.nq:
        raise ValueError(
            f"dump nq={q.shape[1]} != scene nq={jmap.nq}; the dump was recorded "
            f"against a different scene: {raw_path}"
        )

    moved_adrs = np.where(q.max(axis=0) - q.min(axis=0) > MOVE_THRESHOLD)[0]
    robot_idx = (
        robot_override
        if robot_override is not None
        else _detect_executing_robot(jmap, moved_adrs)
    )
    robot_tags = tuple(f"{p}{robot_idx}" for p in ROBOT_PREFIXES)

    channels: dict[str, list[list[float]]] = {}
    fixture_joints: list[str] = []
    moved_names = {jmap.adr_to_name[int(a)] for a in moved_adrs}

    for name, adr in jmap.name_to_adr.items():
        size = jmap.name_to_size[name]
        is_robot = name.startswith(robot_tags)
        if is_robot:
            # keep every channel of the executing robot (pins the start pose)
            channels[name] = q[:, adr:adr + size].tolist()
        elif name in moved_names:
            # a fixture / object joint the skill actually manipulated
            channels[name] = q[:, adr:adr + size].tolist()
            fixture_joints.append(name)

    base_start = {
        jn: float(q[0, jmap.name_to_adr[jn]])
        for jn in (
            f"mobilebase{robot_idx}_joint_mobile_forward",
            f"mobilebase{robot_idx}_joint_mobile_side",
            f"mobilebase{robot_idx}_joint_mobile_yaw",
        )
        if jn in jmap.name_to_adr
    }

    mount = jmap.robot_mounts.get(f"robot{robot_idx}_base")
    if mount is None:
        raise ValueError(
            f"scene has no robot{robot_idx}_base mount body; cannot record "
            "the track's recording metadata"
        )

    return {
        "meta": {
            "skill": skill,
            "robot_index": robot_idx,
            "robot_prefix": f"robot{robot_idx}",
            "scene_xml": jmap_scene_name(jmap),
            "scene_nq": jmap.nq,
            "fixture_joints": sorted(fixture_joints),
            "n_frames": int(q.shape[0]),
            "duration": float(time[-1]),
            "base_start": base_start,
            "source": str(raw_path).replace("\\", "/"),
            # Recording provenance: the exact mount the base channels are
            # relative to, captured from the source model at extraction time.
            # Consumers (skill_generators._retarget_skill_base) re-anchor
            # replays with THIS mount — never a global historical table.
            "recording": {
                "scene_id": jmap.scene_id,
                "model_signature": jmap.model_signature,
                "robot": f"robot{robot_idx}",
                "coordinate_frame": "robot_mount_local",
                "robot_mount": mount,
            },
            "entry_base_pose": jmap.base_world_pose(q[0], robot_idx),
            "exit_base_pose": jmap.base_world_pose(q[-1], robot_idx),
        },
        "time": time,
        "phase": phase,
        "channels": channels,
    }


# scene xml path is only known on the JointMap; expose it lazily
def jmap_scene_name(jmap: JointMap) -> str:
    return getattr(jmap, "_scene_name", "")


def retarget_track(
    jmap: JointMap,
    track: dict,
    src_joint: str,
    dst_joint: str,
    new_skill: str,
) -> dict:
    """Translate a skill track from ``src_joint``'s fixture to ``dst_joint``'s.

    Valid only when the two fixtures are geometrically identical up to a pure
    translation (same orientation, same joint axis, same range) and the world
    translation lies in the executing robot's horizontal driving plane. See
    docs/skill_track_retarget.md for the reasoning and the assumptions.
    """
    if src_joint not in track["channels"]:
        raise ValueError(
            f"source track does not drive '{src_joint}'; it drives "
            f"{track['meta']['fixture_joints']}"
        )

    # --- geometric admissibility checks ---------------------------------
    q_src, q_dst = jmap.joint_body_quat(src_joint), jmap.joint_body_quat(dst_joint)
    quat_err = min(
        np.linalg.norm(q_src - q_dst), np.linalg.norm(q_src + q_dst)
    )
    if quat_err > QUAT_TOL:
        raise ValueError(
            f"fixtures differ in orientation (quat error {quat_err:.4f} > {QUAT_TOL}); "
            "pure-translation retarget would distort the arm motion. Rotational "
            "fixtures (e.g. hinge doors on differently-facing cabinets) are NOT "
            "supported — see docs/skill_track_retarget.md."
        )

    axis_src, axis_dst = jmap.joint_world_axis(src_joint), jmap.joint_world_axis(dst_joint)
    axis_err = min(
        np.linalg.norm(axis_src - axis_dst), np.linalg.norm(axis_src + axis_dst)
    )
    if axis_err > AXIS_TOL:
        raise ValueError(
            f"joint axes differ (error {axis_err:.4f} > {AXIS_TOL}); retarget aborted"
        )

    r_src, r_dst = jmap.joint_range(src_joint), jmap.joint_range(dst_joint)
    if np.linalg.norm(r_src - r_dst) > RANGE_TOL:
        raise ValueError(
            f"joint ranges differ ({r_src} vs {r_dst}); retarget aborted"
        )

    # --- world translation between the two fixtures ---------------------
    delta_world = jmap.joint_world_anchor(dst_joint) - jmap.joint_world_anchor(src_joint)

    # decompose the world delta onto the executing robot's base axes
    robot_idx = track["meta"]["robot_index"]
    fwd_name = f"mobilebase{robot_idx}_joint_mobile_forward"
    side_name = f"mobilebase{robot_idx}_joint_mobile_side"
    fwd_axis = jmap.joint_world_axis(fwd_name)
    side_axis = jmap.joint_world_axis(side_name)
    fwd_off = float(delta_world @ fwd_axis)
    side_off = float(delta_world @ side_axis)
    residual = delta_world - (fwd_off * fwd_axis + side_off * side_axis)
    if np.linalg.norm(residual) > RESIDUAL_TOL:
        raise ValueError(
            f"world delta {np.round(delta_world,4)} is not in the base driving plane "
            f"(residual {np.linalg.norm(residual):.4f} > {RESIDUAL_TOL}); the base "
            "cannot reach the target by translation alone"
        )

    # --- build the retargeted track -------------------------------------
    new = json.loads(json.dumps(track))  # deep copy
    # 1. offset base translation channels by a constant across all frames
    for name, off in ((fwd_name, fwd_off), (side_name, side_off)):
        if name in new["channels"]:
            for frame in new["channels"][name]:
                frame[0] += off
    # 2. move the fixture channel from src joint to dst joint (values unchanged)
    new["channels"][dst_joint] = new["channels"].pop(src_joint)

    meta = new["meta"]
    meta["skill"] = new_skill
    meta["fixture_joints"] = sorted(
        dst_joint if j == src_joint else j for j in meta["fixture_joints"]
    )
    meta["base_start"] = {
        **meta["base_start"],
        fwd_name: meta["base_start"].get(fwd_name, 0.0) + fwd_off,
        side_name: meta["base_start"].get(side_name, 0.0) + side_off,
    }
    meta["retargeted_from"] = {
        "skill": track["meta"]["skill"],
        "src_joint": src_joint,
        "dst_joint": dst_joint,
        "delta_world": [round(v, 6) for v in delta_world.tolist()],
        "base_offset": {"forward": round(fwd_off, 6), "side": round(side_off, 6)},
    }
    # entry/exit world poses shift by the (horizontal) world delta; yaw and the
    # recording mount are unchanged by a pure translation.
    for key in ("entry_base_pose", "exit_base_pose"):
        pose = meta.get(key)
        if pose:
            pose["xy"] = [
                round(pose["xy"][0] + float(delta_world[0]), 6),
                round(pose["xy"][1] + float(delta_world[1]), 6),
            ]
    return new


def reverse_track(track: dict, new_skill: str) -> dict:
    """Time-reverse a skill track (open <-> close synthesis).

    Frames are replayed in opposite order with the original inter-frame
    timing preserved: t'_i = duration - t[n-1-i], so the reversed track is
    monotonic and keeps the source's pacing. base_start is recomputed from
    the new first frame (= the source's final frame).
    """
    new = json.loads(json.dumps(track))  # deep copy
    t = track["time"]
    n = len(t)
    duration = float(t[-1])
    new["time"] = [duration - t[n - 1 - i] for i in range(n)]
    if new.get("phase"):
        new["phase"] = list(reversed(new["phase"]))
    for name in new["channels"]:
        new["channels"][name] = list(reversed(new["channels"][name]))

    meta = new["meta"]
    meta["skill"] = new_skill
    meta["base_start"] = {
        jn: float(new["channels"][jn][0][0])
        for jn in meta.get("base_start", {})
        if jn in new["channels"]
    }
    meta["reversed_from"] = {"skill": track["meta"]["skill"]}
    # playing backwards swaps where the robot starts and ends
    entry, exit_ = meta.get("entry_base_pose"), meta.get("exit_base_pose")
    if entry or exit_:
        meta["entry_base_pose"], meta["exit_base_pose"] = exit_, entry
    return new


def _write_track(track: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(track, indent=1), encoding="utf-8")


def _parse_reverse(spec: str) -> dict:
    parts = spec.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "reverse spec must be robot:SKILL:NEW_SKILL"
        )
    robot, skill, new_skill = parts
    return {"robot": robot, "skill": skill, "new_skill": new_skill}


def _parse_retarget(spec: str) -> dict:
    parts = spec.split(":")
    if len(parts) != 5:
        raise argparse.ArgumentTypeError(
            "retarget spec must be robot:SKILL:SRC_JOINT:DST_JOINT:NEW_SKILL"
        )
    robot, skill, src_joint, dst_joint, new_skill = parts
    return {
        "robot": robot,
        "skill": skill,
        "src_joint": src_joint,
        "dst_joint": dst_joint,
        "new_skill": new_skill,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="directory containing robot0/ robot1/ subfolders of raw dumps",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--robot",
        type=int,
        default=None,
        help="force the executing-robot index instead of auto-detecting",
    )
    parser.add_argument(
        "--retarget",
        type=_parse_retarget,
        action="append",
        default=[],
        help="synthesise a translated skill: robot:SKILL:SRC_JOINT:DST_JOINT:NEW_SKILL",
    )
    parser.add_argument(
        "--reverse",
        type=_parse_reverse,
        action="append",
        default=[],
        help="synthesise the opposite of an open/close skill by time-reversal: robot:SKILL:NEW_SKILL",
    )
    args = parser.parse_args()

    jmap = JointMap(args.scene)
    jmap._scene_name = args.scene.name

    extracted: dict[tuple[str, str], dict] = {}
    for robot_dir in sorted(p for p in args.input_dir.iterdir() if p.is_dir()):
        robot = robot_dir.name
        if robot not in ("robot0", "robot1"):
            continue
        for raw_path in sorted(robot_dir.glob("*.json")):
            skill = raw_path.stem.replace(f"{args.scene.stem}_", "")
            track = extract_track(jmap, raw_path, skill, robot_override=args.robot)
            out = args.output_dir / robot / f"{skill}.track.json"
            _write_track(track, out)
            extracted[(robot, skill)] = track
            fj = track["meta"]["fixture_joints"]
            print(f"[extract] {robot}/{skill}: {len(track['channels'])} channels, fixtures={fj}")

    for spec in args.retarget:
        key = (spec["robot"], spec["skill"])
        if key not in extracted:
            raise SystemExit(f"[retarget] no extracted track for {key}")
        new = retarget_track(
            jmap, extracted[key], spec["src_joint"], spec["dst_joint"], spec["new_skill"]
        )
        out = args.output_dir / spec["robot"] / f"{spec['new_skill']}.track.json"
        _write_track(new, out)
        extracted[(spec["robot"], spec["new_skill"])] = new
        off = new["meta"]["retargeted_from"]["base_offset"]
        print(
            f"[retarget] {spec['robot']}/{spec['skill']} -> {spec['new_skill']}: "
            f"base offset fwd={off['forward']:.3f} side={off['side']:.3f}, "
            f"fixture -> {spec['dst_joint']}"
        )

    for spec in args.reverse:
        key = (spec["robot"], spec["skill"])
        if key not in extracted:
            raise SystemExit(f"[reverse] no extracted track for {key}")
        new = reverse_track(extracted[key], spec["new_skill"])
        out = args.output_dir / spec["robot"] / f"{spec['new_skill']}.track.json"
        _write_track(new, out)
        extracted[(spec["robot"], spec["new_skill"])] = new
        print(
            f"[reverse] {spec['robot']}/{spec['skill']} -> {spec['new_skill']}: "
            f"{new['meta']['n_frames']} frames, duration {new['meta']['duration']:.1f}s"
        )


if __name__ == "__main__":
    main()
