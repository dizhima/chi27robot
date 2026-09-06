"""Robot discovery and manifest descriptor compatibility helpers.

Logical robot ids (``robot0``, ``robot1``, ...) are deliberately independent
from a model's XML namespace.  A PandaOmron may use ``robot2_*`` names while a
Stretch may use ``stretch1_*`` names; consumers use the descriptor to bridge
the two rather than inferring morphology from the logical id.
"""

from __future__ import annotations

import copy
import re

import mujoco

from mujoco_skills.model_signature import robot_mounts


PANDAOMRON = "pandaomron"
STRETCH = "stretch"

_PANDA_ROOT = re.compile(r"^robot(?P<index>\d+)_base$")


def _names(model, object_type, count: int) -> set[str]:
    return {
        name
        for index in range(count)
        if (name := mujoco.mj_id2name(model, object_type, index))
    }


def _body_mount(model, body_name: str) -> dict:
    """Return a root body's declared mount pose in the same shape as legacy data."""
    body_id = mujoco.mj_name2id(
        model, mujoco.mjtObj.mjOBJ_BODY, body_name)
    position = [float(value) for value in model.body_pos[body_id]]
    quaternion = [float(value) for value in model.body_quat[body_id]]
    # This matches model_signature.robot_mounts and is sufficient for the
    # upright mobile bases used by the study scenes.
    import numpy as np

    yaw = float(2.0 * np.arctan2(quaternion[3], quaternion[0]))
    return {"position": position, "quaternion": quaternion, "yaw": yaw}


def _panda_descriptor(model, index: int, mount_body: str,
                      mount: dict, joint_names: set[str]) -> dict:
    namespace = f"robot{index}"
    mobile_namespace = f"mobilebase{index}"
    gripper_namespace = f"gripper{index}_right"
    arm = [f"{namespace}_joint{joint}" for joint in range(1, 8)]
    fingers = [
        f"{gripper_namespace}_finger_joint1",
        f"{gripper_namespace}_finger_joint2",
    ]
    base = [
        f"{mobile_namespace}_joint_mobile_forward",
        f"{mobile_namespace}_joint_mobile_side",
        f"{mobile_namespace}_joint_mobile_yaw",
    ]
    torso = f"{mobile_namespace}_joint_torso_height"
    required = [*arm, *fingers, *base, torso]
    missing = [name for name in required if name not in joint_names]
    descriptor = {
        "index": index,
        "type": PANDAOMRON,
        "namespace": namespace,
        "root_body": mount_body,
        "tracking_body": f"{mobile_namespace}_base",
        # Legacy aliases remain part of the descriptor contract.
        "base_body": mount_body,
        "mobile_base_body": f"{mobile_namespace}_base",
        "mount": mount,
        "arm_joints": arm,
        "torso_joint": torso,
        "finger_joints": fingers,
        "base_joints": base,
        "joints": {
            "base": base,
            "lift": [torso],
            "arm": arm,
            "gripper": fingers,
        },
        "footprint": {"base_clear": 0.35},
    }
    if missing:
        descriptor["unsupported_missing_joints"] = missing
        return descriptor

    def joint_id(name: str) -> int:
        return mujoco.mj_name2id(
            model, mujoco.mjtObj.mjOBJ_JOINT, name)

    finger_ranges = [model.jnt_range[joint_id(name)] for name in fingers]
    descriptor["gripper"] = {
        "open": [
            float(finger_ranges[0][1]), float(finger_ranges[1][0])],
        "closed": [0.0, 0.0],
        "ranges": [
            [float(values[0]), float(values[1])]
            for values in finger_ranges
        ],
    }
    return descriptor


def _stretch_descriptor(model, namespace: str, root_body: str,
                        logical_index: int) -> dict:
    base = [f"{namespace}_base_free_joint"]
    lift = [f"{namespace}_joint_lift"]
    arm = [f"{namespace}_joint_arm_l{index}" for index in range(4)]
    gripper = [
        f"{namespace}_joint_gripper_slide",
        f"{namespace}_joint_gripper_finger_left_open",
        f"{namespace}_joint_gripper_finger_right_open",
    ]
    wheels = [
        f"{namespace}_joint_left_wheel",
        f"{namespace}_joint_right_wheel",
    ]
    return {
        "index": logical_index,
        "type": STRETCH,
        "namespace": namespace,
        "root_body": root_body,
        "tracking_body": root_body,
        "base_body": root_body,
        "mobile_base_body": root_body,
        "mount": _body_mount(model, root_body),
        "base_joints": base,
        "arm_joints": arm,
        "torso_joint": lift[0],
        "finger_joints": gripper,
        "eef_bodies": [
            f"{namespace}_rubber_tip_left",
            f"{namespace}_rubber_tip_right",
        ],
        "joints": {
            "base": base,
            "wheels": wheels,
            "lift": lift,
            "arm": arm,
            "wrist": [f"{namespace}_joint_wrist_yaw"],
            "gripper": gripper,
        },
        "supported_ops": ["navigate", "pick", "place", "reset", "wait"],
        "execution_supported": True,
    }


def discover_robot_descriptors(model) -> dict[str, dict]:
    """Discover known robot instances and assign stable logical ``robotX`` ids.

    Explicit Panda ``robotN_base`` indices are retained.  Other morphologies
    are ordered by their XML namespace and assigned the lowest unclaimed
    logical index, so the mapping is deterministic without making any logical
    id imply a robot type.
    """
    body_names = _names(model, mujoco.mjtObj.mjOBJ_BODY, model.nbody)
    joint_names = _names(model, mujoco.mjtObj.mjOBJ_JOINT, model.njnt)
    mounts = robot_mounts(model)

    robots: dict[str, dict] = {}
    claimed_indices: set[int] = set()
    for mount_body, mount in sorted(mounts.items()):
        match = _PANDA_ROOT.fullmatch(mount_body)
        if match is None:
            continue
        index = int(match.group("index"))
        claimed_indices.add(index)
        robots[f"robot{index}"] = _panda_descriptor(
            model, index, mount_body, mount, joint_names)

    stretch_candidates = []
    for root_body in body_names:
        if not root_body.endswith("_base_link"):
            continue
        namespace = root_body.removesuffix("_base_link")
        signature = {
            f"{namespace}_base_free_joint",
            f"{namespace}_joint_lift",
            *(f"{namespace}_joint_arm_l{index}" for index in range(4)),
            f"{namespace}_joint_gripper_slide",
        }
        if signature <= joint_names:
            stretch_candidates.append((namespace, root_body))

    next_index = 0
    for namespace, root_body in sorted(stretch_candidates):
        while next_index in claimed_indices:
            next_index += 1
        logical_index = next_index
        claimed_indices.add(logical_index)
        robots[f"robot{logical_index}"] = _stretch_descriptor(
            model, namespace, root_body, logical_index)
        next_index += 1

    return dict(sorted(
        robots.items(), key=lambda item: int(item[0].removeprefix("robot"))))


def robot_descriptor(manifest: dict, robot_id: str) -> dict:
    """Return a normalized descriptor, including schema-v2 Panda fallback."""
    raw = (manifest.get("robots") or {}).get(robot_id)
    if not isinstance(raw, dict):
        raise ValueError(f"manifest has no robot descriptor for '{robot_id}'")
    descriptor = copy.deepcopy(raw)
    match = re.fullmatch(r"robot(?P<index>\d+)", robot_id)
    if match is None:
        raise ValueError(
            f"logical robot id '{robot_id}' must have the form robotX")
    index = int(match.group("index"))
    # Existing manifests predate robot morphology.  All of those manifests
    # describe the historical PandaOmron setup, so absence of type is the only
    # backward-compatible default; an unknown explicit type is never guessed.
    descriptor.setdefault("type", PANDAOMRON)
    descriptor.setdefault("index", index)
    descriptor.setdefault("namespace", f"robot{index}")
    return descriptor
