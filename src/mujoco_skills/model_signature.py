"""Stable structural signature of a compiled MuJoCo model.

The signature covers NAMED model structure — body/joint/geom/site/actuator
names, joint types/ranges/qpos addresses, and robot mount transforms — so it
changes whenever a scene is re-exported with different topology or the robots
are re-parked, but is insensitive to cosmetic XML formatting. It is recorded
into skill tracks at extraction time and into the generated skills manifest,
and compared at load time to reject stale scene/track combinations early
instead of failing later inside navigation or IK.
"""

from __future__ import annotations

import hashlib
import json

import mujoco


def _names(model, objtype, count) -> list[str]:
    return [
        mujoco.mj_id2name(model, objtype, i) or f"<anon:{i}>"
        for i in range(count)
    ]


def robot_mounts(model) -> dict[str, dict]:
    """World mount transform of every `robot{N}_base` body in the model."""
    import numpy as np

    mounts: dict[str, dict] = {}
    for b in range(model.nbody):
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, b) or ""
        if not (name.startswith("robot") and name.endswith("_base")):
            continue
        pos = [float(v) for v in model.body_pos[b]]
        quat = [float(v) for v in model.body_quat[b]]
        yaw = float(2.0 * np.arctan2(quat[3], quat[0]))
        mounts[name] = {"position": pos, "quaternion": quat, "yaw": yaw}
    return mounts


def model_signature(model) -> str:
    """sha256 over the named structural content of a compiled model."""
    payload = {
        "nq": int(model.nq),
        "nv": int(model.nv),
        "bodies": _names(model, mujoco.mjtObj.mjOBJ_BODY, model.nbody),
        "joints": [
            {
                "name": mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, j) or f"<anon:{j}>",
                "type": int(model.jnt_type[j]),
                "qposadr": int(model.jnt_qposadr[j]),
                "range": [float(model.jnt_range[j][0]), float(model.jnt_range[j][1])],
            }
            for j in range(model.njnt)
        ],
        "geoms": _names(model, mujoco.mjtObj.mjOBJ_GEOM, model.ngeom),
        "sites": _names(model, mujoco.mjtObj.mjOBJ_SITE, model.nsite),
        "actuators": _names(model, mujoco.mjtObj.mjOBJ_ACTUATOR, model.nu),
        "robot_mounts": robot_mounts(model),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
