"""Clone canonical tracks between isomorphic robots in one MuJoCo scene.

Run the expensive dataset replay only on one source robot, finish canonical
post-processing there, then invoke this module once per finalized source track:

    uv run --with mujoco==3.10.0 python -m \
      mujoco_skills.pipeline.retarget_robot_tracks \
      --scene frontend/public/assets/robocasa/layout049_sorting.xml \
      --source frontend/public/trajectories/layout049_sorting/tracks/robot0/OpenFridge.track.json \
      --all-robots \
      --output-dir frontend/public/trajectories/layout049_sorting/tracks

Arm, torso, gripper, and fixture values are copied after renaming robot-owned
channels. Mobile-base values are solved through the target scene's actual
MuJoCo forward kinematics so every clone reproduces the source world-space
base and end-effector path even when robot mounts have different positions or
yaws. A mount-only 2-D transform is insufficient for PandaOmron because its
root and moving-base bodies have a fixed internal offset.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path

import mujoco
import numpy as np

from mujoco_skills.model_signature import robot_mounts


ROBOT_CHANNEL_PREFIXES = ("mobilebase", "robot", "gripper")


def _rename_channel(name: str, source_index: int, target_index: int) -> str:
    for prefix in ROBOT_CHANNEL_PREFIXES:
        old = f"{prefix}{source_index}_"
        if name.startswith(old):
            return f"{prefix}{target_index}_{name[len(old):]}"
    return name


def _wrap_angle(value: float) -> float:
    return (value + math.pi) % (2.0 * math.pi) - math.pi


def _base_joint_names(robot_index: int) -> tuple[str, str, str]:
    return (
        f"mobilebase{robot_index}_joint_mobile_forward",
        f"mobilebase{robot_index}_joint_mobile_side",
        f"mobilebase{robot_index}_joint_mobile_yaw",
    )


def _joint_qpos_width(model: mujoco.MjModel, joint_id: int) -> int:
    joint_type = int(model.jnt_type[joint_id])
    return 7 if joint_type == 0 else (4 if joint_type == 1 else 1)


def _base_joint_addresses(
    model: mujoco.MjModel, robot_index: int
) -> tuple[int, int, int]:
    addresses = []
    for name in _base_joint_names(robot_index):
        joint_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
        if joint_id < 0:
            raise ValueError(f"scene has no joint {name!r}")
        addresses.append(int(model.jnt_qposadr[joint_id]))
    return tuple(addresses)


def _mobile_base_pose(
    model: mujoco.MjModel, data: mujoco.MjData, robot_index: int
) -> tuple[float, float, float]:
    body_id = mujoco.mj_name2id(
        model, mujoco.mjtObj.mjOBJ_BODY, f"mobilebase{robot_index}_base"
    )
    if body_id < 0:
        raise ValueError(f"scene has no mobilebase{robot_index}_base body")
    rotation = data.xmat[body_id].reshape(3, 3)
    return (
        float(data.xpos[body_id, 0]),
        float(data.xpos[body_id, 1]),
        math.atan2(float(rotation[1, 0]), float(rotation[0, 0])),
    )


def validate_compatible_robot(
    track: dict, model: mujoco.MjModel, target_robot: str
) -> None:
    """Require identical driven joint types, widths, and limits."""
    meta = track["meta"]
    source_index = int(meta["robot_index"])
    target_index = int(target_robot.removeprefix("robot"))
    errors = []
    checked = 0
    for source_name in track["channels"]:
        if not any(
            source_name.startswith(f"{prefix}{source_index}_")
            for prefix in ROBOT_CHANNEL_PREFIXES
        ):
            continue
        target_name = _rename_channel(source_name, source_index, target_index)
        source_id = mujoco.mj_name2id(
            model, mujoco.mjtObj.mjOBJ_JOINT, source_name
        )
        target_id = mujoco.mj_name2id(
            model, mujoco.mjtObj.mjOBJ_JOINT, target_name
        )
        if source_id < 0 or target_id < 0:
            errors.append(f"missing joint pair {source_name!r} -> {target_name!r}")
            continue
        checked += 1
        if int(model.jnt_type[source_id]) != int(model.jnt_type[target_id]):
            errors.append(f"joint type differs for {source_name!r} -> {target_name!r}")
        if _joint_qpos_width(model, source_id) != _joint_qpos_width(model, target_id):
            errors.append(f"qpos width differs for {source_name!r} -> {target_name!r}")
        if not np.allclose(
            model.jnt_range[source_id], model.jnt_range[target_id], atol=1e-9
        ):
            errors.append(f"joint range differs for {source_name!r} -> {target_name!r}")
    if checked == 0:
        errors.append("source track has no robot-owned channels")
    if errors:
        raise ValueError(
            f"{target_robot} is not compatible with {meta['robot_prefix']}: "
            + "; ".join(errors)
        )


def _source_world_base_poses(
    track: dict, model: mujoco.MjModel
) -> list[tuple[float, float, float]]:
    source_index = int(track["meta"]["robot_index"])
    names = _base_joint_names(source_index)
    addresses = _base_joint_addresses(model, source_index)
    data = mujoco.MjData(model)
    poses = []
    for values in zip(*(track["channels"][name] for name in names), strict=True):
        data.qpos[:] = model.qpos0
        for address, value in zip(addresses, values, strict=True):
            data.qpos[address] = float(value[0])
        mujoco.mj_forward(model, data)
        poses.append(_mobile_base_pose(model, data, source_index))
    return poses


def _solve_target_base_values(
    model: mujoco.MjModel,
    target_index: int,
    desired_poses: list[tuple[float, float, float]],
) -> tuple[list[list[float]], list[list[float]], list[list[float]]]:
    addresses = _base_joint_addresses(model, target_index)
    data = mujoco.MjData(model)
    values = np.asarray([model.qpos0[address] for address in addresses], dtype=float)
    outputs: list[list[list[float]]] = [[], [], []]

    def evaluate(candidate: np.ndarray, desired) -> np.ndarray:
        data.qpos[:] = model.qpos0
        data.qpos[list(addresses)] = candidate
        mujoco.mj_forward(model, data)
        residual = np.asarray(_mobile_base_pose(model, data, target_index)) - np.asarray(desired)
        residual[2] = _wrap_angle(float(residual[2]))
        return residual

    for frame_index, desired in enumerate(desired_poses):
        for _ in range(8):
            residual = evaluate(values, desired)
            if float(np.linalg.norm(residual)) <= 1e-10:
                break
            jacobian = np.zeros((3, 3), dtype=float)
            epsilon = 1e-6
            for column in range(3):
                perturbed = values.copy()
                perturbed[column] += epsilon
                delta = evaluate(perturbed, desired) - residual
                delta[2] = _wrap_angle(float(delta[2]))
                jacobian[:, column] = delta / epsilon
            values += np.linalg.lstsq(jacobian, -residual, rcond=None)[0]
        final_residual = evaluate(values, desired)
        if float(np.linalg.norm(final_residual)) > 1e-8:
            raise ValueError(
                f"could not encode source world base pose for robot{target_index} "
                f"at frame {frame_index}: residual={final_residual.tolist()}"
            )
        for output, value in zip(outputs, values, strict=True):
            output.append([float(value)])
    return outputs[0], outputs[1], outputs[2]


def retarget_track(track: dict, model: mujoco.MjModel, target_robot: str) -> dict:
    """Return a robot-specific copy preserving the source world-space path."""
    result = copy.deepcopy(track)
    meta = result["meta"]
    source_robot = meta["robot_prefix"]
    source_index = int(meta["robot_index"])
    target_index = int(target_robot.removeprefix("robot"))
    if source_robot == target_robot:
        raise ValueError("source and target robot must differ")

    mounts = {
        name.removesuffix("_base"): mount
        for name, mount in robot_mounts(model).items()
    }
    if source_robot not in mounts or target_robot not in mounts:
        raise ValueError(f"scene has no mount for {source_robot} or {target_robot}")
    validate_compatible_robot(track, model, target_robot)

    source_base_names = _base_joint_names(source_index)
    target_base_names = _base_joint_names(target_index)
    target_base_values = _solve_target_base_values(
        model, target_index, _source_world_base_poses(track, model)
    )

    renamed = {
        _rename_channel(name, source_index, target_index): values
        for name, values in result["channels"].items()
        if name not in source_base_names
    }
    renamed.update(dict(zip(target_base_names, target_base_values, strict=True)))
    result["channels"] = {
        _rename_channel(name, source_index, target_index): renamed[
            _rename_channel(name, source_index, target_index)
        ]
        for name in track["channels"]
    }

    meta["robot_index"] = target_index
    meta["robot_prefix"] = target_robot
    meta["base_start"] = {
        name: result["channels"][name][0][0] for name in target_base_names
    }
    recording = meta.setdefault("recording", {})
    recording["robot"] = target_robot
    recording["robot_mount"] = copy.deepcopy(mounts[target_robot])
    meta["robot_retargeted_from"] = source_robot
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    targets = parser.add_mutually_exclusive_group(required=True)
    targets.add_argument("--target-robot", action="append")
    targets.add_argument("--all-robots", action="store_true")
    outputs = parser.add_mutually_exclusive_group(required=True)
    outputs.add_argument("--output", type=Path)
    outputs.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    model = mujoco.MjModel.from_xml_path(str(args.scene))
    track = json.loads(args.source.read_text(encoding="utf-8"))
    source_robot = track["meta"]["robot_prefix"]
    scene_robots = [
        name.removesuffix("_base") for name in sorted(robot_mounts(model))
    ]
    target_robots = (
        [robot for robot in scene_robots if robot != source_robot]
        if args.all_robots
        else args.target_robot
    )
    if args.output is not None and len(target_robots) != 1:
        parser.error("--output requires exactly one target robot")

    for target_robot in target_robots:
        if target_robot not in scene_robots:
            parser.error(f"unknown target robot: {target_robot}")
        result = retarget_track(track, model, target_robot)
        output = (
            args.output
            if args.output is not None
            else args.output_dir / target_robot / args.source.name
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(
            f"wrote {output}: {source_robot} -> {target_robot} "
            f"({result['meta']['n_frames']} frames)"
        )


if __name__ == "__main__":
    main()
