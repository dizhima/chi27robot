r"""Append a collision-checked handle-release tail to canonical OpenFridge tracks.

The canonical demos end with the gripper at the refrigerator handle.  A normal
joint-linear reset from that pose sweeps the wrist through the door, forcing the
compiler into a slow local RRT.  This tool synthesizes a short Cartesian retreat
toward the robot base while the refrigerator joints remain fixed open.

Dry-run (default):
    uv run python -m mujoco_skills.pipeline.append_openfridge_release

Back up and replace both canonical tracks after validation:
    uv run python -m mujoco_skills.pipeline.append_openfridge_release --apply

Target another exported scene without editing this module:
    uv run python -m mujoco_skills.pipeline.append_openfridge_release \
        --scene frontend/public/assets/robocasa/layout042_sorting.xml \
        --tracks-dir frontend/public/trajectories/layout042_sorting/tracks \
        --apply
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

import mujoco
import numpy as np

from mujoco_skills.skills import skill_generators as sg


ROOT = Path(__file__).resolve().parents[3]
PUBLIC = ROOT / "frontend" / "public"
SCENE = PUBLIC / "assets" / "robocasa" / "layout042_study.xml"
TRACKS = PUBLIC / "trajectories" / "layout042_study" / "tracks"


def _q_from_frame(rig, track, frame=-1):
    q = rig.model.qpos0.copy()
    for name, values in track["channels"].items():
        try:
            adr = rig.jadr(name)
        except Exception:  # fixture/scene channels may not resolve as robot joints
            continue
        value = values[frame]
        q[adr:adr + len(value)] = value
    return q


def _geom_name(model, geom):
    return mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, int(geom)) or ""


def _body_ancestry_names(model, body):
    names = []
    while body > 0:
        names.append(
            mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, int(body)) or "")
        body = int(model.body_parentid[body])
    return names


def _external_contacts(rig, q):
    model, data = rig.model, rig.data
    root = rig.base_body
    while int(model.body_parentid[root]) > 0:
        root = int(model.body_parentid[root])
    robot_geoms = sg._body_subtree_geoms(model, root)
    data.qpos[:] = q
    mujoco.mj_forward(model, data)
    contacts = []
    for index in range(data.ncon):
        contact = data.contact[index]
        g1, g2 = int(contact.geom1), int(contact.geom2)
        in1, in2 = g1 in robot_geoms, g2 in robot_geoms
        if in1 == in2:
            continue
        robot_geom, other_geom = (g1, g2) if in1 else (g2, g1)
        other_body = int(model.geom_bodyid[other_geom])
        ancestry = _body_ancestry_names(model, other_body)
        contacts.append({
            "pair": (robot_geom, other_geom),
            "robot_geom": _geom_name(model, robot_geom),
            "other_geom": _geom_name(model, other_geom),
            "other_ancestry": ancestry,
            "fridge": "fridge" in " ".join(ancestry).lower(),
            "distance": float(contact.dist),
        })
    return contacts


def _unit(vector):
    vector = np.asarray(vector, dtype=float)
    norm = float(np.linalg.norm(vector))
    return None if norm < 1e-8 else vector / norm


def _candidate_directions(rig, q):
    eef, _ = rig.eef_pose(q)
    base = np.asarray(rig.base_xy(q), dtype=float)
    toward_base = _unit(np.array([base[0] - eef[0], base[1] - eef[1], 0.0]))
    directions = [toward_base]

    # Small vertical biases help the fingers clear a horizontal handle without
    # changing the dominant outward retreat direction.
    if toward_base is not None:
        directions.extend([
            _unit(toward_base + np.array([0.0, 0.0, 0.25])),
            _unit(toward_base + np.array([0.0, 0.0, -0.20])),
        ])

    forward = rig.base_forward(q)
    directions.extend([
        _unit(np.array([-forward[0], -forward[1], 0.0])),
        _unit(np.array([forward[0], forward[1], 0.0])),
        np.array([1.0, 0.0, 0.0]),
        np.array([-1.0, 0.0, 0.0]),
        np.array([0.0, 1.0, 0.0]),
        np.array([0.0, -1.0, 0.0]),
    ])

    unique = []
    for direction in directions:
        if direction is None:
            continue
        if not any(abs(float(np.dot(direction, old))) > 0.995 for old in unique):
            unique.append(direction)
    return unique


def _linear_ready_is_clear(rig, q):
    ready = q.copy()
    rig.apply_ready(ready)
    rig.set_fingers(ready, rig.finger_open)
    adrs = [rig.TORSO, *rig.ARM, *rig.FINGERS]
    resolution = [
        sg.RRT_TORSO_EDGE_RES,
        *([sg.RESET_ARM_EDGE_RES] * len(rig.ARM)),
        0.01,
        0.01,
    ]
    return sg._qpos_edge_collision_free(
        q, ready, adrs, resolution, sg._robot_collision_checker(rig, q))


def _release_waypoints(rig, start, distance, steps):
    start_eef, start_quat = rig.eef_pose(start)
    # Preserve the recorded wrist frame while translating away from the handle.
    data = rig.data
    data.qpos[:] = start
    mujoco.mj_forward(rig.model, data)
    rotation = data.xmat[rig.eef].reshape(3, 3).copy()
    approach = rotation @ sg.APPROACH_LOCAL
    up = rotation @ sg.GRIPPER_UP_LOCAL
    baseline_contacts = _external_contacts(rig, start)
    baseline_pairs = {contact["pair"] for contact in baseline_contacts}
    start_fridge_depth = min(
        (contact["distance"] for contact in baseline_contacts if contact["fridge"]),
        default=0.0,
    )

    def release_valid(q):
        """Permit only the recorded gripper/fridge overlap while pulling out.

        The source demo starts with the hand collision geom about 3 cm inside
        overlapping visual/clearance door geoms. As it exits, contact pair IDs
        can change between those duplicate door geoms. Allow that narrow class
        without allowing wrist/arm, floor, or unrelated-scene contacts to grow.
        """
        for contact in _external_contacts(rig, q):
            if contact["fridge"]:
                if not contact["robot_geom"].startswith(
                        f"gripper{rig.robot}_right_"):
                    return False
                if contact["distance"] < start_fridge_depth - 0.003:
                    return False
            elif contact["pair"] not in baseline_pairs:
                return False
        return True
    arm_adrs = [rig.TORSO, *rig.ARM, *rig.FINGERS]
    arm_resolution = [
        sg.RRT_TORSO_EDGE_RES,
        *([sg.RESET_ARM_EDGE_RES] * len(rig.ARM)),
        0.01,
        0.01,
    ]

    candidates = []
    failures = {"ik": 0, "collision": 0}
    for direction_index, direction in enumerate(_candidate_directions(rig, start)):
        # Prefer preserving the recorded wrist frame. Position-only IK is a
        # fallback for this short release because the handle pose can sit at an
        # orientation singularity even though a safe translational escape exists.
        for position_only in (False, True):
            waypoints = [start.copy()]
            previous = start.copy()
            errors = []
            valid_path = True
            for amount in np.linspace(distance / steps, distance, steps):
                candidate = previous.copy()
                target = start_eef + float(amount) * direction
                error = rig.ik_arm(
                    candidate,
                    target,
                    top_down=False,
                    approach_world=(None if position_only else approach),
                    up_world=(None if position_only else up),
                )
                errors.append(error)
                if error > 0.012:
                    failures["ik"] += 1
                    valid_path = False
                    break
                if not sg._qpos_edge_collision_free(
                        previous, candidate, arm_adrs, arm_resolution,
                        release_valid):
                    failures["collision"] += 1
                    valid_path = False
                    break
                waypoints.append(candidate)
                previous = candidate
            if not valid_path:
                continue

            final_contacts = _external_contacts(rig, waypoints[-1])
            fridge_contacts = [contact for contact in final_contacts if contact["fridge"]]
            if fridge_contacts:
                failures["collision"] += 1
                continue
            linear_ready = _linear_ready_is_clear(rig, waypoints[-1])
            joint_travel = float(np.sum(np.abs(
                waypoints[-1][rig.ARM] - start[rig.ARM])))
            candidates.append((
                not linear_ready,
                len(fridge_contacts),
                position_only,
                max(errors),
                joint_travel,
                direction_index,
                direction,
                waypoints,
                final_contacts,
            ))

    if not candidates:
        raise RuntimeError(
            "no collision-checked Cartesian handle release found; "
            f"failures={failures}")
    candidates.sort(key=lambda candidate: candidate[:6])
    best = candidates[0]
    if best[0]:
        raise RuntimeError(
            "handle release candidates exist, but none permit a linear ready reset")
    return best[7], best[6], best[3], best[8]


def _append_release(raw, release, *, distance, direction, max_ik_error):
    out = copy.deepcopy(raw)
    append_count = len(release["time"]) - 1
    base_names = {
        f"mobilebase{out['meta']['robot_index']}_joint_mobile_forward",
        f"mobilebase{out['meta']['robot_index']}_joint_mobile_side",
        f"mobilebase{out['meta']['robot_index']}_joint_mobile_yaw",
    }
    for name, values in out["channels"].items():
        if name in release["channels"] and name not in base_names:
            values.extend(copy.deepcopy(release["channels"][name][1:]))
        else:
            values.extend([copy.deepcopy(values[-1]) for _ in range(append_count)])

    end_time = float(out["time"][-1])
    out["time"].extend([
        round(end_time + float(value), 5)
        for value in release["time"][1:]
    ])
    out["phase"].extend(["release_handle"] * append_count)
    out["meta"]["n_frames"] = len(out["time"])
    out["meta"]["duration"] = out["time"][-1]
    out["meta"]["release_handle"] = {
        "generator": "cartesian_ik_toward_base",
        "distance": round(float(distance), 6),
        "duration": round(float(release["time"][-1]), 6),
        "direction": [round(float(value), 8) for value in direction],
        "max_ik_error": round(float(max_ik_error), 8),
        "collision_policy": "baseline_contacts_only",
    }
    return out


def build_augmented_track(
    robot, *, distance, steps, duration, scene=SCENE, tracks=TRACKS,
):
    scene = Path(scene)
    tracks = Path(tracks)
    path = tracks / f"robot{robot}" / "OpenFridge.track.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("meta", {}).get("release_handle"):
        raise RuntimeError(f"{path} already contains a release_handle tail")

    rig = sg.SceneRig(str(scene), robot=robot)
    rig.set_ready(sg.load_ready(tracks))
    retargeted = sg._retarget_skill_base(rig, copy.deepcopy(raw))
    start = _q_from_frame(rig, retargeted)
    start_contacts = _external_contacts(rig, start)
    waypoints, direction, max_error, final_contacts = _release_waypoints(
        rig, start, distance, steps)
    release = sg.build_track(
        rig,
        "OpenFridge_release_handle",
        waypoints,
        [duration / steps] * steps,
        phase_label="release_handle",
    )
    augmented = _append_release(
        raw,
        release,
        distance=distance,
        direction=direction,
        max_ik_error=max_error,
    )

    # Re-load the actual canonical output and assert its terminal pose still
    # admits the fast reset path used by compile_robot.
    check_rig = sg.SceneRig(str(scene), robot=robot)
    check_rig.set_ready(sg.load_ready(tracks))
    check_track = sg._retarget_skill_base(check_rig, copy.deepcopy(augmented))
    check_q = _q_from_frame(check_rig, check_track)
    # Authored articulation resets use the standard 0.18 m retreat. Validate
    # that exact production path rather than gen_reset's broader standalone
    # default.
    reset, _ = sg.gen_reset(check_rig, check_q, retreat=0.18)
    if reset["meta"]["arm_reset_mode"] != "linear":
        raise RuntimeError(
            f"robot{robot} augmented track still requires "
            f"{reset['meta']['arm_reset_mode']} reset")

    report = {
        "robot": f"robot{robot}",
        "path": str(path),
        "old_frames": len(raw["time"]),
        "new_frames": len(augmented["time"]),
        "old_duration": raw["meta"]["duration"],
        "new_duration": augmented["meta"]["duration"],
        "release_distance": distance,
        "release_direction": [round(float(value), 5) for value in direction],
        "max_ik_error": max_error,
        "start_fridge_contacts": sum(c["fridge"] for c in start_contacts),
        "final_fridge_contacts": sum(c["fridge"] for c in final_contacts),
        "reset_mode": reset["meta"]["arm_reset_mode"],
        "reset_retreat": reset["meta"]["retreat_distance"],
    }
    return path, raw, augmented, report


def _write_json_atomic(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--scene", type=Path, default=SCENE,
        help="Scene XML used to solve and collision-check the release tail.")
    parser.add_argument(
        "--tracks-dir", type=Path, default=TRACKS,
        help="Canonical track root containing robotN/ directories.")
    parser.add_argument(
        "--robot", type=int, action="append", default=None,
        help="robot index to process; repeat as needed. By default, discover "
             "every robotN/OpenFridge.track.json under --tracks-dir.")
    parser.add_argument("--distance", type=float, default=0.16)
    parser.add_argument("--steps", type=int, default=4)
    parser.add_argument("--duration", type=float, default=0.8)
    args = parser.parse_args()

    robots = args.robot
    if robots is None:
        robots = sorted(
            int(path.parent.name.removeprefix("robot"))
            for path in args.tracks_dir.glob("robot*/OpenFridge.track.json")
            if path.parent.name.removeprefix("robot").isdigit()
        )
    if not robots:
        raise SystemExit(
            f"no robotN/OpenFridge.track.json found under {args.tracks_dir}"
        )

    generated = [
        build_augmented_track(
            robot,
            distance=args.distance,
            steps=args.steps,
            duration=args.duration,
            scene=args.scene,
            tracks=args.tracks_dir,
        )
        for robot in robots
    ]
    for _, _, _, report in generated:
        print(json.dumps(report, ensure_ascii=False))

    if not args.apply:
        print("dry-run only; pass --apply to back up and replace the tracks")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = (
        args.tracks_dir / "_backups" / f"openfridge_pre_release_{stamp}"
    )
    for path, _, augmented, _ in generated:
        backup = backup_root / path.parent.name / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        _write_json_atomic(path, augmented)
    print(f"backup={backup_root}")


if __name__ == "__main__":
    main()
