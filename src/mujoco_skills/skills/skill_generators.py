r"""Parametric motion-skill generators (navigate / pick / place / wait).

Produces the same name-addressed channel-track JSON as the articulation skills,
so generated motions drop straight into the frontend SchedulePlayer/timeline.

Design (see the conversation notes):
* Real IK reach: the arm is solved with a position-only damped-least-squares
  step against the actual scene model (mujoco 3.10.0, same as the frontend), so
  the gripper genuinely reaches each object. Grasp *physics* is simplified to a
  kinematic attach — the held object's free joint is slaved to the gripper via
  forward kinematics, no contact forces.
* Granular steps, not a fused transport: navigate / pick / place / wait each
  emit their own track. A robot's steps are compiled in order, threading the
  base pose and the currently-held object, so `pick` attaches, `navigate`/`wait`
  carry, and `place` releases. This keeps the coordination surface (e.g. an LLM
  inserting a `wait` between two navigates) visible.

Run the self-test (generates the mug_1 -> sink sequence for robot0 and writes
tracks) with:
    uv run --with mujoco==3.10.0 python -m mujoco_skills.skills.skill_generators
"""

from __future__ import annotations

import bisect
import copy
import contextvars
import hashlib
import json
import math
import os
import struct
import sys
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import mujoco

from mujoco_skills.pipeline.build_navigation_grid import (
    ensure_navigation_grid,
    navigation_grid_paths,
)
from mujoco_skills.pipeline.navigation_planner import plan_grid_route

FPS = 30.0

# Compile profiling mode.  Set MJSKILL_COMPILE_PROFILE in .env to one of:
#   detail (default): every step plus the final TOTAL line
#   total:            only the final TOTAL line
#   off:              no compile-profile lines
# MJSKILL_PROFILE=0 remains supported as the legacy spelling for "off".
# Lines go to stderr with a "[compile-profile]" prefix so they show up in the
# skill_service console/logs without polluting the JSON returned to the frontend.
def _compile_profile_mode():
    mode = os.environ.get("MJSKILL_COMPILE_PROFILE")
    if mode is None:
        legacy = os.environ.get("MJSKILL_PROFILE", "1").strip().lower()
        return "off" if legacy in ("0", "", "false", "off") else "detail"

    mode = mode.strip().lower()
    if mode in ("detail", "full", "1", "true", "on"):
        return "detail"
    if mode in ("total", "summary"):
        return "total"
    if mode in ("off", "0", "false", "", "none"):
        return "off"
    return "detail"


_COMPILE_PROFILE_MODE = _compile_profile_mode()
_COMPILE_PROFILE_CONTEXT = contextvars.ContextVar(
    "mjskill_compile_profile_context", default=None)


@contextmanager
def compile_profile_context(request_id, plan_key):
    """Attach a service request/plan id to every profile line in this thread."""
    token = _COMPILE_PROFILE_CONTEXT.set(
        f"request={request_id} plan={str(plan_key)[:12]}")
    try:
        yield
    finally:
        _COMPILE_PROFILE_CONTEXT.reset(token)

# A resting object only needs full contact simulation while its container joint
# is moving, plus a short tail in which any motion induced by the final contact
# can settle.  The rest of an articulation demo may contain several seconds of
# the robot withdrawing or driving away while the drawer/door itself is already
# stationary.  Simulating that tail at the scene's 500 Hz physics rate changes
# no scripted fixture state and is needlessly expensive.
_PHYSICS_REPLAY_TRIM = os.environ.get(
    "MJSKILL_PHYSICS_REPLAY_TRIM", "1") not in ("0", "", "false")
PHYSICS_REPLAY_FIXTURE_MOTION_EPS = 1e-6
PHYSICS_REPLAY_SETTLE_TAIL_SEC = 0.4
try:
    # Contact replay is an offline trajectory bake, not the frontend runtime.
    # A 2x step retains sub-frame contact fidelity while halving mj_step calls;
    # set this to 1 to reproduce the scene model's native timestep exactly.
    PHYSICS_REPLAY_STEP_MULTIPLIER = max(
        1, int(os.environ.get("MJSKILL_PHYSICS_REPLAY_STEP_MULTIPLIER", "2")))
except ValueError:
    PHYSICS_REPLAY_STEP_MULTIPLIER = 2


def _profile_detail(op, tr):
    """One-line breakdown of where a generated track spent its compile time.

    For front reach-in places this surfaces which ingress path actually ran
    (straight_ik vs the RRT fallback) plus the RRT/Cartesian sub-timings, so a
    slow compile can be attributed to a specific stage.
    """
    meta = tr.get("meta", {}) if isinstance(tr, dict) else {}
    if meta.get("physics_replay_skipped"):
        return (
            "physics_replay skipped "
            f"reason={meta.get('physics_replay_skip_reason')} "
            f"objects={meta.get('physics_object_count', 0)}")
    if op == "place" and meta.get("planner", "").startswith("rrt_connect"):
        parts = [f"ingress={meta.get('ingress_planner')}"]
        if meta.get("rrt_iterations"):
            parts.append(f"rrt_iters={meta['rrt_iterations']}")
        if meta.get("rrt_search_time_sec"):
            parts.append(f"rrt={meta['rrt_search_time_sec']}s")
        if meta.get("planner_collision_time_sec"):
            parts.append(f"collision={meta['planner_collision_time_sec']}s")
        if meta.get("rrt_collision_checks"):
            parts.append(f"checks={meta['rrt_collision_checks']}")
        return " ".join(parts)
    if op == "reset":
        # A `local_rrt` arm reset means the direct joint interpolation back to
        # ready collided (e.g. arm still inside an open fridge) and a sampling
        # planner had to run — the usual reason a reset is slow.
        parts = [f"arm_mode={meta.get('arm_reset_mode')}"]
        if meta.get("retreat_distance") is not None:
            parts.append(f"retreat={meta['retreat_distance']:.2f}m")
        if meta.get("reset_rrt_iterations"):
            parts.append(f"rrt_iters={meta['reset_rrt_iterations']}")
        return " ".join(parts)
    if meta.get("physics_substeps"):
        # An Open/Close replay carrying a resting object: cost is a full
        # mj_step contact simulation over the recorded demo, not planning.
        skipped = meta.get("physics_skipped_substeps", 0)
        suffix = f" skipped={skipped}" if skipped else ""
        return (f"physics_replay substeps={meta['physics_substeps']}{suffix} "
                f"sim_time={meta.get('physics_sim_time_sec')}s")
    return ""


def _profile_log(msg, *, level="detail"):
    if _COMPILE_PROFILE_MODE == "detail" or (
            _COMPILE_PROFILE_MODE == "total" and level == "total"):
        context = _COMPILE_PROFILE_CONTEXT.get()
        prefix = f"[{context}] " if context else ""
        print(
            f"[compile-profile] {prefix}{msg}",
            file=sys.stderr,
            flush=True,
        )

# Top-down grasp: align the gripper's local approach axis to world -Z. If the
# gripper ends up pointing up/sideways, flip APPROACH_LOCAL's sign.
APPROACH_LOCAL = np.array([0.0, 0.0, 1.0])
# Horizontal side grasps use local +Y as the world-up reference. The previous
# local +X reference produced the right approach direction but left the hand
# rolled by 90 degrees, so the fingers looked vertical in playback.
GRIPPER_UP_LOCAL = np.array([0.0, 1.0, 0.0])
WORLD_DOWN = np.array([0.0, 0.0, -1.0])
WORLD_UP = np.array([0.0, 0.0, 1.0])
ORI_WEIGHT = 0.6  # orientation vs position weight in the 6-DOF IK

RING_GAP = 0.75   # ring-lane offset outside the island footprint, metres
BASE_SPEED = 0.6  # m/s
YAW_SPEED = 1.5   # rad/s
# Adjacent skills often end and begin at the same fixture pose with only a few
# centimetres of numerical/retargeting drift.  Treat that as one local base
# transition so a tiny displacement does not acquire an arbitrary travel
# heading and make the robot turn away, move, then turn back.
NEAR_NAV_COLLAPSE_DISTANCE = 0.05
# A completed plan echoes the compiler-derived navigate standoff back into the
# next request.  Keep that stable for tiny placement-marker corrections, but a
# meaningful move of the drop point must invalidate the old stance and solve a
# new one around the edited point.
PLACEMENT_STANDOFF_RECOMPUTE_DISTANCE = 0.15

RRT_SEED = 42042
RRT_STEP = 0.22
RRT_EDGE_RES = 0.035
RRT_VALIDATION_EDGE_RES = 0.035
RRT_TORSO_STEP = 0.10
RRT_TORSO_SAMPLE_RES = 0.10
RRT_TORSO_EDGE_RES = 0.02
RRT_MAX_ITERS = 5000
RRT_SHORTCUT_ITERS = 180
RRT_ARM_SPEED = 0.65
RESET_RETREAT_DEFAULT = 0.50   # desired backward standoff before arm reset;
                               # actual distance is the largest that stays
                               # collision-free (see gen_reset). A bigger
                               # standoff pulls the arm clear of an open
                               # container so the arm reset is a straight line
                               # instead of an expensive local RRT.
RESET_RETREAT_STEP = 0.05      # granularity of the feasible-retreat scan, m
RESET_BASE_EDGE_RES = 0.02
RESET_ARM_EDGE_RES = 0.01
RESET_YAW_MATCH_TOL = 0.10
REACHIN_FRONT_DISTANCE = 0.28
REACHIN_LIFT_HEIGHT = 0.03
REACHIN_CARTESIAN_STEP = 0.025
# Deterministic, scene-calibrated continuation seeds for narrow-container
# reach-in. These are single seeds, not candidate banks: failure is immediate.
REACHIN_ENTRY_SEED_BY_OBJECT = {
    "cake_1": {
        "torso": 0.24393019590396614,
        "arm": [
            -2.8973,
            -1.0437646297517291,
            -0.025405470256135487,
            -2.6808031699384838,
            -0.5163490354902041,
            0.09063963088958449,
            -2.8491665766044725,
        ],
    },
}


def _wrap_pi(x):
    return (x + np.pi) % (2 * np.pi) - np.pi


def _ring_corners(bbox, gap):
    xmin, xmax, ymin, ymax = bbox
    return [
        np.array([xmin - gap, ymin - gap]),
        np.array([xmax + gap, ymin - gap]),
        np.array([xmax + gap, ymax + gap]),
        np.array([xmin - gap, ymax + gap]),
    ]


def _seg_crosses_aabb(a, b, bbox, pad=0.0):
    """Does segment a->b intersect the (padded) island AABB? (slab test)"""
    xmin, xmax, ymin, ymax = bbox
    xmin -= pad; xmax += pad; ymin -= pad; ymax += pad
    d = b - a
    t0, t1 = 0.0, 1.0
    for lo, hi, o, dd in ((xmin, xmax, a[0], d[0]), (ymin, ymax, a[1], d[1])):
        if abs(dd) < 1e-9:
            if o < lo or o > hi:
                return False
        else:
            ta, tb = (lo - o) / dd, (hi - o) / dd
            if ta > tb:
                ta, tb = tb, ta
            t0 = max(t0, ta); t1 = min(t1, tb)
            if t0 > t1:
                return False
    return True


def _route_xy(a, s, bbox, gap):
    """Intermediate floor waypoints from a to s. Empty if the straight line
    doesn't cross the island; otherwise route around the island's ring lane."""
    if not _seg_crosses_aabb(a, s, bbox, pad=0.1):
        return []
    corners = _ring_corners(bbox, gap)
    ei = min(range(4), key=lambda k: np.linalg.norm(corners[k] - a))
    xi = min(range(4), key=lambda k: np.linalg.norm(corners[k] - s))
    # walk the 4-corner loop from ei to xi, both directions, pick shorter
    def path(idxs):
        return [corners[k] for k in idxs]
    fwd_idx, k = [], ei
    while True:
        fwd_idx.append(k)
        if k == xi:
            break
        k = (k + 1) % 4
    bwd_idx, k = [], ei
    while True:
        bwd_idx.append(k)
        if k == xi:
            break
        k = (k - 1) % 4
    def length(idxs):
        pts = [a] + path(idxs) + [s]
        return sum(np.linalg.norm(pts[i + 1] - pts[i]) for i in range(len(pts) - 1))
    best = fwd_idx if length(fwd_idx) <= length(bwd_idx) else bwd_idx
    return path(best)


# ---------------------------------------------------------------- quaternions
def quat_mul(a, b):
    w0, x0, y0, z0 = a
    w1, x1, y1, z1 = b
    return np.array([
        w0 * w1 - x0 * x1 - y0 * y1 - z0 * z1,
        w0 * x1 + x0 * w1 + y0 * z1 - z0 * y1,
        w0 * y1 - x0 * z1 + y0 * w1 + z0 * x1,
        w0 * z1 + x0 * y1 - y0 * x1 + z0 * w1,
    ])


def quat_conj(q):
    return np.array([q[0], -q[1], -q[2], -q[3]])


def quat_rot(q, v):
    qv = np.array([0.0, *v])
    return quat_mul(quat_mul(q, qv), quat_conj(q))[1:]


def quat_align_vectors(current, target):
    """Shortest quaternion rotating one direction onto another."""
    current = np.asarray(current, dtype=float)
    target = np.asarray(target, dtype=float)
    current /= np.linalg.norm(current)
    target /= np.linalg.norm(target)
    dot = float(np.clip(np.dot(current, target), -1.0, 1.0))
    if dot > 1.0 - 1e-10:
        return np.array([1.0, 0.0, 0.0, 0.0])
    if dot < -1.0 + 1e-10:
        reference = (
            np.array([1.0, 0.0, 0.0])
            if abs(float(current[0])) < 0.9
            else np.array([0.0, 1.0, 0.0])
        )
        axis = np.cross(current, reference)
        axis /= np.linalg.norm(axis)
        return np.array([0.0, *axis])
    result = np.array([1.0 + dot, *np.cross(current, target)])
    return result / np.linalg.norm(result)


def quat_slerp(a, b, alpha):
    """Shortest-path interpolation between unit wxyz quaternions."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    dot = float(np.dot(a, b))
    if dot < 0.0:
        b = -b
        dot = -dot
    dot = float(np.clip(dot, -1.0, 1.0))
    if dot > 0.9995:
        result = (1.0 - alpha) * a + alpha * b
        return result / np.linalg.norm(result)
    angle = np.arccos(dot)
    sine = np.sin(angle)
    return (
        np.sin((1.0 - alpha) * angle) / sine * a
        + np.sin(alpha * angle) / sine * b
    )


class SceneRig:
    """Kinematics helpers for one PandaOmron mobile manipulator in a scene."""

    def __init__(self, scene_xml: str, robot: int = 0):
        self.scene_path = Path(scene_xml).resolve()
        self.model = mujoco.MjModel.from_xml_path(scene_xml)
        self.data = mujoco.MjData(self.model)
        self.robot = robot
        self.scene_name = Path(scene_xml).name

        def jadr(name):
            return int(self.model.jnt_qposadr[
                mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)])

        def jdof(name):
            return int(self.model.jnt_dofadr[
                mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)])

        p = f"robot{robot}"
        mb = f"mobilebase{robot}"
        gr = f"gripper{robot}_right"
        self.FWD, self.SIDE, self.YAW = (
            jadr(f"{mb}_joint_mobile_forward"),
            jadr(f"{mb}_joint_mobile_side"),
            jadr(f"{mb}_joint_mobile_yaw"),
        )
        self.yaw_joint = mujoco.mj_name2id(
            self.model, mujoco.mjtObj.mjOBJ_JOINT,
            f"{mb}_joint_mobile_yaw")
        self.TORSO = jadr(f"{mb}_joint_torso_height")
        self.ARM = [jadr(f"{p}_joint{i}") for i in range(1, 8)]
        self.ARM_DOF = [jdof(f"{p}_joint{i}") for i in range(1, 8)]
        _arm_jid = [mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT,
                                      f"{p}_joint{i}") for i in range(1, 8)]
        self.ARM_LIMITED = [bool(self.model.jnt_limited[j]) for j in _arm_jid]
        self.ARM_RANGE = [self.model.jnt_range[j].copy() for j in _arm_jid]
        self.FINGERS = [jadr(f"{gr}_finger_joint1"), jadr(f"{gr}_finger_joint2")]
        self.eef = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, f"{gr}_eef")
        self.base_body = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, f"{mb}_base")

        # names of every joint owned by this robot (channels a track may write)
        self.robot_joint_names = []
        for j in range(self.model.njnt):
            n = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_JOINT, j)
            if n.startswith((f"{p}_", f"{mb}_", f"gripper{robot}_")):
                self.robot_joint_names.append(n)

        mujoco.mj_forward(self.model, self.data)
        self.home_base_xy = self.data.xpos[self.base_body][:2].copy()
        # The two finger joints are mirror-signed (ranges [0,0.04] and [-0.04,0]):
        # open = fully spread (range extremes), closed = nearly together.
        r1 = self.model.jnt_range[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, f"{gr}_finger_joint1")]
        r2 = self.model.jnt_range[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, f"{gr}_finger_joint2")]
        self.finger_open = [float(r1[1]), float(r2[0])]     # e.g. [ 0.04, -0.04]
        self.finger_closed = [float(r1[0]) + 0.006, float(r2[1]) - 0.006]  # ~[0.006,-0.006]
        self.ready = None  # {arm:[7], torso, fingers:[2]}; set via set_ready()

    def set_fingers(self, q, state):
        q[self.FINGERS[0]], q[self.FINGERS[1]] = state[0], state[1]

    # -- ready pose --------------------------------------------------------
    def set_ready(self, ready):
        self.ready = ready

    def apply_ready(self, q):
        """Set this robot's arm + torso to the shared ready pose (base untouched)."""
        for a, v in zip(self.ARM, self.ready["arm"]):
            q[a] = v
        q[self.TORSO] = self.ready["torso"]
        return q

    def set_island(self, bbox):
        self.island_bbox = tuple(bbox)

    # -- world lookups -----------------------------------------------------
    def body_xy(self, body_name):
        i = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, body_name)
        return self.data.xpos[i][:3].copy()

    def surface_z(self, x, y, top=1.8):
        """World z of the first surface hit by a ray dropped straight down at
        (x, y). Cast against the rest pose (qpos0) so a robot arm parked over the
        spot can't shadow the counter; restores scratch state after. Returns None
        if the ray misses everything."""
        d = self.data
        saved = d.qpos.copy()
        d.qpos[:] = self.model.qpos0
        mujoco.mj_forward(self.model, d)
        pnt = np.array([float(x), float(y), float(top)])
        vec = np.array([0.0, 0.0, -1.0])
        gid = np.zeros(1, dtype=np.int32)
        dist = mujoco.mj_ray(self.model, d, pnt, vec, None, 1, -1, gid)
        d.qpos[:] = saved
        mujoco.mj_forward(self.model, d)
        if gid[0] < 0 or dist < 0:
            return None
        return float(top - dist)

    def base_xy(self, q):
        self.data.qpos[:] = q
        mujoco.mj_forward(self.model, self.data)
        return self.data.xpos[self.base_body][:2].copy()

    def chassis_xy(self, q):
        """World XY of the mobile base's physical yaw pivot.

        ``base_xy`` is the arm-mount body origin and remains the coordinate
        contract used by recorded manipulation skills.  Navigation must use
        the yaw-joint anchor instead: keeping the arm mount fixed while yawing
        makes the chassis center orbit around it and can sweep the tail into
        obstacles.
        """
        self.data.qpos[:] = q
        mujoco.mj_forward(self.model, self.data)
        return self.data.xanchor[self.yaw_joint][:2].copy()

    def jadr(self, name):
        return int(self.model.jnt_qposadr[
            mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)])

    # -- base placement ----------------------------------------------------
    def standoff_xy(self, target_xy, dist=0.55):
        """A base XY `dist` from the target toward the robot's home side."""
        to_home = self.home_base_xy - target_xy
        to_home = to_home / np.linalg.norm(to_home)
        return target_xy + to_home * dist

    def place_base(self, q, target_xy, face_xy):
        """Set base channels in qpos `q` so the base sits at target_xy facing face_xy."""
        d = self.data
        d.qpos[:] = q
        # forward-dir yaw at home
        d.qpos[self.YAW] = 0.0
        d.qpos[self.FWD] = 0.0
        d.qpos[self.SIDE] = 0.0
        mujoco.mj_forward(self.model, d)
        b0 = d.xpos[self.base_body][:2].copy()
        d.qpos[self.FWD] = 1.0
        mujoco.mj_forward(self.model, d)
        fdir = (d.xpos[self.base_body][:2] - b0)
        fwd_yaw = np.arctan2(fdir[1], fdir[0])
        d.qpos[self.FWD] = 0.0

        yaw = np.arctan2(face_xy[1] - target_xy[1], face_xy[0] - target_xy[0]) - fwd_yaw
        d.qpos[self.YAW] = yaw
        mujoco.mj_forward(self.model, d)
        c0 = d.xpos[self.base_body][:2].copy()
        d.qpos[self.FWD] = 1.0
        mujoco.mj_forward(self.model, d)
        fd = d.xpos[self.base_body][:2] - c0
        d.qpos[self.FWD] = 0.0
        d.qpos[self.SIDE] = 1.0
        mujoco.mj_forward(self.model, d)
        sd = d.xpos[self.base_body][:2] - c0
        d.qpos[self.SIDE] = 0.0
        A = np.array([fd, sd]).T
        f, s = np.linalg.solve(A, target_xy - c0)
        q[self.FWD], q[self.SIDE], q[self.YAW] = f, s, yaw
        return q

    def base_forward(self, q):
        """World-space vector of the robot's visual forward direction.

        The mobile ``forward`` slide joint is world-aligned in this MJCF, so
        probing that qpos channel does *not* rotate with yaw.  Reset retreats
        must instead use the base body's local +X axis transformed to world.
        """
        d = self.data
        d.qpos[:] = q
        mujoco.mj_forward(self.model, d)
        direction = d.xmat[self.base_body].reshape(3, 3)[:2, 0].copy()
        norm = float(np.linalg.norm(direction))
        if norm < 1e-9:
            raise ValueError("mobile base visual forward axis has zero world direction")
        return direction / norm

    def place_base_preserve_yaw(self, q, target_xy):
        """Move the base center to target_xy without changing its yaw."""
        d = self.data
        target_xy = np.asarray(target_xy, dtype=float)
        yaw = float(q[self.YAW])
        probe = q.copy()
        probe[self.YAW] = yaw
        probe[self.FWD] = 0.0
        probe[self.SIDE] = 0.0
        d.qpos[:] = probe
        mujoco.mj_forward(self.model, d)
        c0 = d.xpos[self.base_body][:2].copy()

        probe[self.FWD] = 1.0
        d.qpos[:] = probe
        mujoco.mj_forward(self.model, d)
        fd = d.xpos[self.base_body][:2] - c0
        probe[self.FWD] = 0.0
        probe[self.SIDE] = 1.0
        d.qpos[:] = probe
        mujoco.mj_forward(self.model, d)
        sd = d.xpos[self.base_body][:2] - c0
        f, s = np.linalg.solve(np.array([fd, sd]).T, target_xy - c0)
        q[self.FWD], q[self.SIDE], q[self.YAW] = f, s, yaw
        return q

    def place_chassis_preserve_yaw(self, q, target_xy):
        """Move the physical yaw pivot to ``target_xy``, preserving yaw."""
        d = self.data
        target_xy = np.asarray(target_xy, dtype=float)
        yaw = float(q[self.YAW])
        probe = q.copy()
        probe[self.YAW] = yaw
        probe[self.FWD] = 0.0
        probe[self.SIDE] = 0.0
        d.qpos[:] = probe
        mujoco.mj_forward(self.model, d)
        c0 = d.xanchor[self.yaw_joint][:2].copy()

        probe[self.FWD] = 1.0
        d.qpos[:] = probe
        mujoco.mj_forward(self.model, d)
        fd = d.xanchor[self.yaw_joint][:2] - c0
        probe[self.FWD] = 0.0
        probe[self.SIDE] = 1.0
        d.qpos[:] = probe
        mujoco.mj_forward(self.model, d)
        sd = d.xanchor[self.yaw_joint][:2] - c0
        f, s = np.linalg.solve(np.array([fd, sd]).T, target_xy - c0)
        q[self.FWD], q[self.SIDE], q[self.YAW] = f, s, yaw
        return q

    def place_chassis(self, q, target_xy, face_xy):
        """Place the physical yaw pivot at target_xy facing face_xy."""
        target_xy = np.asarray(target_xy, dtype=float)
        face_xy = np.asarray(face_xy, dtype=float)
        direction = face_xy - target_xy
        if float(np.linalg.norm(direction)) < 1e-9:
            raise ValueError("chassis facing point must differ from target")
        current_forward = self.base_forward(q)
        desired_yaw = (
            float(q[self.YAW])
            + _wrap_pi(
                np.arctan2(direction[1], direction[0])
                - np.arctan2(current_forward[1], current_forward[0])
            )
        )
        q[self.YAW] = desired_yaw
        # Changing yaw above moves the joint anchor in world coordinates unless
        # the slide channels are compensated.  Re-anchor it at the requested
        # chassis center after selecting the facing.
        self.place_chassis_preserve_yaw(q, target_xy)
        return q

    # -- arm IK ------------------------------------------------------------
    def ik_arm(self, q, target_pos, iters=300, tol=4e-3, top_down=True,
               approach_world=None, up_world=None):
        """DLS IK on the seven arm joints.

        `top_down=True` preserves the historical constraint: gripper local +Z
        points along world -Z. A caller can instead pass `approach_world` (and
        optionally `up_world`) for a horizontal/full approach frame. With
        neither constraint (`top_down=False`, approach_world=None), IK is
        position-only, as used by the cabinet reach-in goal sampler.
        """
        d = self.data
        seed = [q[a] for a in self.ARM]     # arm pose IK starts from
        target_approach = None
        target_up = None
        if top_down:
            target_approach = WORLD_DOWN
        elif approach_world is not None:
            target_approach = np.asarray(approach_world, dtype=float)
            target_approach /= np.linalg.norm(target_approach)
        if up_world is not None:
            target_up = np.asarray(up_world, dtype=float)
            target_up /= np.linalg.norm(target_up)
        d.qpos[:] = q
        jacp = np.zeros((3, self.model.nv))
        jacr = np.zeros((3, self.model.nv))
        for _ in range(iters):
            mujoco.mj_forward(self.model, d)
            errp = target_pos - d.xpos[self.eef]
            if target_approach is not None:
                R = d.xmat[self.eef].reshape(3, 3)
                approach = R @ APPROACH_LOCAL
                errr = np.cross(approach, target_approach)
                if target_up is not None:
                    gripper_up = R @ GRIPPER_UP_LOCAL
                    errr += np.cross(gripper_up, target_up)
                if np.linalg.norm(errp) < tol and np.linalg.norm(errr) < 0.04:
                    break
                mujoco.mj_jacBody(self.model, d, jacp, jacr, self.eef)
                J = np.vstack([jacp[:, self.ARM_DOF], ORI_WEIGHT * jacr[:, self.ARM_DOF]])
                e = np.concatenate([errp, ORI_WEIGHT * errr])
            else:
                if np.linalg.norm(errp) < tol:
                    break
                mujoco.mj_jacBody(self.model, d, jacp, None, self.eef)
                J = jacp[:, self.ARM_DOF]
                e = errp
            dq = J.T @ np.linalg.solve(J @ J.T + 0.05**2 * np.eye(J.shape[0]), e)
            for k, a in enumerate(self.ARM):
                d.qpos[a] += dq[k]
        # Pull each arm joint to the equivalent angle nearest the seed. Top-down
        # IK leaves the wrist roll (joint7) unconstrained — a redundant DOF — so
        # over many DLS steps it can wind up several turns, then unwind on
        # playback (the visible gripper spin). Wrapping to within pi of the seed
        # is orientation-preserving (mod 2*pi) and turns the short way; clamp
        # limited joints so wrapping can't push one past its range. Mirrors the
        # base-yaw unwrap in gen_navigate.
        for k, a in enumerate(self.ARM):
            val = seed[k] + _wrap_pi(d.qpos[a] - seed[k])
            if self.ARM_LIMITED[k]:
                lo, hi = self.ARM_RANGE[k]
                val = min(max(val, lo), hi)
            q[a] = val
            d.qpos[a] = val
        mujoco.mj_forward(self.model, d)
        return float(np.linalg.norm(target_pos - d.xpos[self.eef]))

    def eef_pose(self, q):
        d = self.data
        d.qpos[:] = q
        mujoco.mj_forward(self.model, d)
        return d.xpos[self.eef].copy(), d.xquat[self.eef].copy()


# ---------------------------------------------------------------- track build
def _obj_joint(obj):
    return f"{obj}_joint0"


def _rest_obj_pose(rig, obj):
    a = rig.jadr(_obj_joint(obj))
    q = rig.model.qpos0[a:a + 7].copy()
    return q[:3].copy(), q[3:7].copy()


def _rest_support_clearance(rig, obj):
    """Height of an object's body origin above whatever it rests on in the
    authored initial scene — a ray dropped straight down from the origin,
    ignoring the object's own geoms.  A baked ``study_init`` keyframe takes
    precedence over qpos0 because objects authored inside an initially-open
    drawer/cabinet are positioned relative to that open fixture.  Measuring
    them against the closed qpos0 fixture can make the ray hit structure far
    below the object and turn a centimetre-scale clearance into ~0.7 m.

    Placing the origin this far above a target surface reproduces the object's
    rest appearance exactly. This sidesteps guessing the mesh bottom: geom
    AABBs aren't tight for these meshes (they read ~1-3 cm low and differ per
    object, which is what left placements floating by different amounts)."""
    m, d = rig.model, rig.data
    saved = d.qpos.copy()
    key = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_KEY, "study_init")
    d.qpos[:] = m.key_qpos[key] if key >= 0 else m.qpos0
    mujoco.mj_forward(m, d)
    bid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, f"{obj}_main")
    o = d.xpos[bid].copy()
    pnt = np.array([float(o[0]), float(o[1]), float(o[2]) + 0.001])
    vec = np.array([0.0, 0.0, -1.0])
    gid = np.zeros(1, dtype=np.int32)
    dist = mujoco.mj_ray(m, d, pnt, vec, None, 1, bid, gid)
    d.qpos[:] = saved
    mujoco.mj_forward(m, d)
    if gid[0] < 0 or dist < 0:
        return 0.0
    support_z = float(o[2]) + 0.001 - float(dist)
    return max(0.0, float(o[2]) - support_z)


def _grasp_offset(rig, obj, q_at_grasp):
    """Relative pose of the object in the gripper frame, captured at grasp."""
    ep, eq = rig.eef_pose(q_at_grasp)
    op, oq = _rest_obj_pose(rig, obj)
    off_pos = quat_rot(quat_conj(eq), op - ep)
    off_quat = quat_mul(quat_conj(eq), oq)
    return off_pos, off_quat


def _carried_pose(rig, q_frame, off_pos, off_quat):
    ep, eq = rig.eef_pose(q_frame)
    return ep + quat_rot(eq, off_pos), quat_mul(eq, off_quat)


def build_track(rig, skill, waypoints, durations, obj=None, phase_label=None):
    """Interpolate waypoints into a channel track. `obj` (optional) is a dict:
    {name, attach_from_seg, off_pos, off_quat, static_pose} — the object channel
    is slaved to the gripper on/after `attach_from_seg`, else held at static_pose."""
    names = list(rig.robot_joint_names)
    channels = {n: [] for n in names}
    obj_adr = None
    if obj:
        obj_adr = rig.jadr(_obj_joint(obj["name"]))
        channels[_obj_joint(obj["name"])] = []
    time, phase = [], []
    adrs = {n: rig.jadr(n) for n in names}

    def obj_pose_for(seg, q, alpha=0.0):
        # attached (slaved to gripper) on [attach_from_seg, detach_from_seg);
        # otherwise held at static_pose (rest before pick, drop point after place).
        attach = obj["attach_from_seg"]
        detach = obj.get("detach_from_seg", seg_count + 1)
        if attach <= seg < detach:
            pose = _carried_pose(rig, q, obj["off_pos"], obj["off_quat"])
            if seg == obj.get("orient_to_static_from_seg"):
                pose = (
                    pose[0],
                    quat_slerp(pose[1], obj["static_pose"][1], alpha),
                )
            return pose
        return obj["static_pose"]

    t = 0.0
    seg_count = len(durations)
    for seg in range(seg_count):
        n = max(1, int(round(durations[seg] * FPS)))
        for f in range(n):
            a = f / n
            q = (1 - a) * waypoints[seg] + a * waypoints[seg + 1]
            for name in names:
                channels[name].append([float(q[adrs[name]])])
            if obj:
                op, oq = obj_pose_for(seg, q, a)
                channels[_obj_joint(obj["name"])].append([*map(float, op), *map(float, oq)])
            time.append(round(t, 5))
            phase.append(phase_label or skill)
            t += 1.0 / FPS
    # final frame
    qf = waypoints[-1]
    for name in names:
        channels[name].append([float(qf[adrs[name]])])
    if obj:
        op, oq = obj_pose_for(seg_count - 1, qf)
        channels[_obj_joint(obj["name"])].append([*map(float, op), *map(float, oq)])
    time.append(round(t, 5))
    phase.append(phase_label or skill)

    return {
        "meta": {
            "skill": skill,
            "robot_index": rig.robot,
            "scene_nq": int(rig.model.nq),
            "fixture_joints": [],  # motion skills touch no articulation fixture
            "n_frames": len(time),
            "duration": time[-1],
        },
        "time": time,
        "phase": phase,
        "channels": channels,
    }


# ---------------------------------------------------------------- generators
def load_standoffs(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        name: {"standoff_xy": np.array(v["standoff_xy"]), "face_xy": np.array(v["face_xy"])}
        for name, v in data["targets"].items()
    }


def load_ready(tracks_dir):
    tr = json.loads((Path(tracks_dir) / "robot0" / "OpenFridge.track.json").read_text("utf-8"))
    f0 = {n: vals[0] for n, vals in tr["channels"].items()}
    return {
        "arm": [f0[f"robot0_joint{i}"][0] for i in range(1, 8)],
        "torso": f0["mobilebase0_joint_torso_height"][0],
        "fingers": [f0["gripper0_right_finger_joint1"][0], f0["gripper0_right_finger_joint2"][0]],
    }


def gen_navigate(rig, q_start, standoff, carrying=None, label="navigate",
                 via_points=None, waypoints=None, preserve_yaw=False, final_q=None,
                 align_final_yaw=True, arrival_yaw=None,
                 standoff_is_chassis=False):
    """Route the base to ``standoff`` on the cached static navigation grid.

    ``via_points`` are authored constraints. ``waypoints`` is accepted only as
    a legacy alias; generated metadata stores the complete derived path under
    ``route`` so a completed plan cannot feed its own output back as constraints.
    Each route leg normally rotates in place then translates.
    With preserve_yaw="auto" and a direct route whose requested final facing
    already matches the current yaw, the omnidirectional base translates
    directly without the initial turn. `final_q` optionally reaches the exact
    entry pose of a following articulation replay.
    """
    base_standoff = np.asarray(standoff["standoff_xy"], dtype=float)
    face = np.asarray(standoff["face_xy"], dtype=float)

    authored_vias = via_points if via_points is not None else waypoints
    authored_vias = [] if authored_vias is None else list(authored_vias)

    # arm ready + preserve grip through the move
    base_tmpl = q_start.copy()
    # A horizontal side pick returns a deliberate carry pose. Preserve it while
    # carrying; top-down picks already return the historical ready pose, so this
    # is byte-for-byte equivalent for existing pick/navigation plans.
    if carrying is None:
        rig.apply_ready(base_tmpl)
    for a in rig.FINGERS:
        base_tmpl[a] = q_start[a]

    def unwrap(pose, ref):
        # keep yaw within pi of ref so linear interpolation turns the short way
        pose[rig.YAW] = ref[rig.YAW] + _wrap_pi(pose[rig.YAW] - ref[rig.YAW])

    # A following articulation replay must end at its exact recorded entry
    # pose; debug/free-space navigates may instead keep the last travel-facing
    # yaw by setting align_final_yaw=False.
    if arrival_yaw is not None:
        align_final_yaw = False
    align_final_yaw = bool(align_final_yaw) or final_q is not None
    # Recorded manipulation entries and authored standoffs describe the arm
    # mount (`base_xy`), but the route planner must move the physical chassis
    # center.  First solve the legacy destination contract, then route to the
    # yaw-pivot position of that exact pose.  This preserves manipulation
    # alignment while preventing turns around the arm pedestal.
    destination = base_tmpl.copy()
    if final_q is not None:
        destination[rig.FWD] = final_q[rig.FWD]
        destination[rig.SIDE] = final_q[rig.SIDE]
        destination[rig.YAW] = final_q[rig.YAW]
    elif standoff_is_chassis:
        if arrival_yaw is not None:
            destination[rig.YAW] = float(arrival_yaw)
            rig.place_chassis_preserve_yaw(destination, base_standoff)
        elif (align_final_yaw
              and float(np.linalg.norm(face - base_standoff)) >= 1e-9):
            rig.place_chassis(destination, base_standoff, face)
        else:
            rig.place_chassis_preserve_yaw(destination, base_standoff)
    elif arrival_yaw is not None:
        destination[rig.YAW] = float(arrival_yaw)
        rig.place_base_preserve_yaw(destination, base_standoff)
    elif align_final_yaw:
        rig.place_base(destination, base_standoff, face)
    else:
        rig.place_chassis_preserve_yaw(destination, base_standoff)

    A = rig.chassis_xy(q_start)
    S = rig.chassis_xy(destination)
    nav_grid = ensure_navigation_grid(rig.scene_path)
    full_route = plan_grid_route(nav_grid, A, S, authored_vias)
    route = full_route[1:-1]
    visit = full_route[1:]

    # A very short direct route is an alignment correction, not meaningful
    # travel.  Keep the current yaw while translating and only then adopt the
    # requested final pose.  In particular this folds Open -> source navigate
    # and place -> navigate-for-Close transitions without deleting their plan
    # steps (and therefore without changing scheduler/reservation bookkeeping).
    # Authored vias and arrival-yaw moves retain the ordinary
    # rotate-then-translate behaviour.  A* can produce an inflated-grid detour
    # even when the physical endpoints are virtually identical, so accept the
    # fold based on real chassis distance and validate its exact base edges
    # against MuJoCo before using it.
    near_direct = (
        arrival_yaw is None
        and not authored_vias
        and float(np.linalg.norm(S - A)) <= NEAR_NAV_COLLAPSE_DISTANCE
    )
    if near_direct:
        poses = [base_tmpl.copy()]
        durs = []

        translated = base_tmpl.copy()
        rig.place_chassis_preserve_yaw(translated, S)
        poses.append(translated)
        durs.append(max(0.2, float(np.linalg.norm(S - A)) / BASE_SPEED))

        aligned = translated.copy()
        aligned[rig.FWD] = destination[rig.FWD]
        aligned[rig.SIDE] = destination[rig.SIDE]
        aligned[rig.YAW] = destination[rig.YAW]
        unwrap(aligned, translated)
        if float(np.max(np.abs(aligned - translated))) > 1e-9:
            poses.append(aligned)
            durs.append(max(
                0.3,
                abs(float(aligned[rig.YAW] - translated[rig.YAW]))
                / YAW_SPEED,
            ))

        valid = _robot_collision_checker(rig, base_tmpl)
        base_edges_safe = all(
            _qpos_edge_collision_free(
                edge_start, edge_end,
                [rig.FWD, rig.SIDE, rig.YAW],
                [RESET_BASE_EDGE_RES, RESET_BASE_EDGE_RES, 0.05],
                valid,
            )
            for edge_start, edge_end in zip(poses, poses[1:])
        )
        if base_edges_safe:
            endpoint = poses[-1]
            if final_q is not None:
                entry = final_q.copy()
                entry[rig.YAW] = endpoint[rig.YAW] + _wrap_pi(
                    entry[rig.YAW] - endpoint[rig.YAW])
                if float(np.max(np.abs(entry - endpoint))) > 1e-9:
                    poses.append(entry)
                    durs.append(max(
                        0.3,
                        abs(float(entry[rig.YAW] - endpoint[rig.YAW]))
                        / YAW_SPEED,
                        float(np.max(np.abs(
                            entry[rig.ARM] - endpoint[rig.ARM])))
                        / RRT_ARM_SPEED,
                    ))

            obj = {**carrying, "attach_from_seg": 0} if carrying else None
            track = build_track(rig, label, poses, durs, obj)
            track["meta"]["route"] = [
                [float(A[0]), float(A[1])],
                [float(S[0]), float(S[1])],
            ]
            track["meta"]["via_points"] = []
            track["meta"]["face_xy"] = [float(face[0]), float(face[1])]
            track["meta"]["collapsed_near_navigation"] = True
            return track, poses[-1]

    requested_preserve = (
        align_final_yaw
        and (preserve_yaw is True or preserve_yaw == "auto"))
    if requested_preserve and not route:
        desired = destination.copy()
        unwrap(desired, base_tmpl)
        if final_q is not None:
            desired[rig.YAW] = (
                base_tmpl[rig.YAW]
                + _wrap_pi(final_q[rig.YAW] - base_tmpl[rig.YAW])
            )
        yaw_gap = abs(float(desired[rig.YAW] - base_tmpl[rig.YAW]))
        if yaw_gap <= RESET_YAW_MATCH_TOL:
            trans = base_tmpl.copy()
            rig.place_chassis_preserve_yaw(trans, S)
            valid = _robot_collision_checker(rig, base_tmpl)
            if not _qpos_edge_collision_free(
                    base_tmpl, trans, [rig.FWD, rig.SIDE, rig.YAW],
                    [RESET_BASE_EDGE_RES, RESET_BASE_EDGE_RES, 0.05], valid):
                raise ValueError(
                    f"preserve-yaw navigate to '{label}' collides")
            poses = [base_tmpl, trans]
            durs = [max(0.2, np.linalg.norm(S - A) / BASE_SPEED)]
            endpoint = desired
            if final_q is not None:
                endpoint = final_q.copy()
                endpoint[rig.YAW] = (
                    trans[rig.YAW]
                    + _wrap_pi(endpoint[rig.YAW] - trans[rig.YAW])
                )
            if float(np.max(np.abs(endpoint - trans))) > 1e-9:
                poses.append(endpoint)
                durs.append(max(
                    0.3,
                    abs(float(endpoint[rig.YAW] - trans[rig.YAW])) / YAW_SPEED,
                    float(np.max(np.abs(
                        endpoint[rig.ARM] - trans[rig.ARM]))) / RRT_ARM_SPEED,
                ))
            obj = {**carrying, "attach_from_seg": 0} if carrying else None
            track = build_track(rig, label, poses, durs, obj)
            track["meta"]["route"] = [
                [float(A[0]), float(A[1])],
                [float(S[0]), float(S[1])],
            ]
            track["meta"]["via_points"] = [
                [float(point[0]), float(point[1])]
                for point in authored_vias
            ]
            track["meta"]["face_xy"] = [float(face[0]), float(face[1])]
            track["meta"]["preserved_yaw_translation"] = True
            return track, poses[-1]

    poses = [base_tmpl.copy()]
    durs = []
    prev = A
    for i, wp in enumerate(visit):
        final_leg_with_arrival_yaw = (
            arrival_yaw is not None and i == len(visit) - 1)
        # Normally face the travel direction. A home/clearing move may instead
        # adopt its collision-safe arrival yaw before the final translation, so
        # it reaches the endpoint without turning in place there.
        rot = poses[-1].copy()
        if final_leg_with_arrival_yaw:
            rot[rig.YAW] = (
                poses[-1][rig.YAW]
                + _wrap_pi(float(arrival_yaw) - poses[-1][rig.YAW]))
            rig.place_chassis_preserve_yaw(rot, prev)
        else:
            rig.place_chassis(rot, prev, wp)
            unwrap(rot, poses[-1])
        durs.append(max(0.3, abs(rot[rig.YAW] - poses[-1][rig.YAW]) / YAW_SPEED))
        poses.append(rot)
        # Translate to wp keeping the selected yaw.
        trans = rot.copy()
        rig.place_chassis_preserve_yaw(trans, wp)
        durs.append(max(0.4, np.linalg.norm(wp - prev) / BASE_SPEED))
        poses.append(trans)
        prev = wp
    # Normally rotate at the destination to face the target. A clearing/home
    # navigate can deliberately keep the final leg's travel-facing yaw so the
    # base does not perform a potentially colliding turn in place.
    final = poses[-1].copy()
    if align_final_yaw:
        final[rig.FWD] = destination[rig.FWD]
        final[rig.SIDE] = destination[rig.SIDE]
        final[rig.YAW] = destination[rig.YAW]
        unwrap(final, poses[-1])
        durs.append(max(
            0.3, abs(final[rig.YAW] - poses[-1][rig.YAW]) / YAW_SPEED))
        poses.append(final)
    if final_q is not None:
        entry = final_q.copy()
        entry[rig.YAW] = final[rig.YAW] + _wrap_pi(
            entry[rig.YAW] - final[rig.YAW])
        poses.append(entry)
        durs.append(max(
            0.3,
            float(np.max(np.abs(entry[rig.ARM] - final[rig.ARM])))
            / RRT_ARM_SPEED,
        ))

    obj = {**carrying, "attach_from_seg": 0} if carrying else None
    track = build_track(rig, label, poses, durs, obj)
    track["meta"]["route"] = [
        [float(point[0]), float(point[1])] for point in full_route
    ]
    track["meta"]["via_points"] = [
        [float(point[0]), float(point[1])]
        for point in authored_vias
    ]
    track["meta"]["face_xy"] = [float(face[0]), float(face[1])]
    track["meta"]["preserved_yaw_translation"] = False
    track["meta"]["aligned_final_yaw"] = align_final_yaw
    track["meta"]["arrival_yaw"] = (
        None if arrival_yaw is None else float(arrival_yaw))
    return track, poses[-1]


def _solve_approach_ik(rig, q_seed, target_pos, approach_world, up_world,
                       attempts=64, seed=42042, candidate_valid=None):
    """Deterministic multi-seed pose IK used by horizontal side picks.

    Candidates must meet the Cartesian tolerances before scoring. Among them,
    prefer the solution closest to q_seed and optionally reject candidates whose
    q_seed->candidate edge is not collision-free.
    """
    # Equivalent robots solving the same world-space grasp should see the same
    # candidate bank.  Adding ``rig.robot`` here made robot1 select a different
    # redundant-arm branch even when its base pose and target exactly matched
    # robot0's, which could turn a proven fridge grasp into a handle collision.
    rng = np.random.default_rng(seed)
    target = np.asarray(target_pos, dtype=float)
    approach_target = np.asarray(approach_world, dtype=float)
    approach_target /= np.linalg.norm(approach_target)
    up_target = np.asarray(up_world, dtype=float)
    up_target /= np.linalg.norm(up_target)
    best = None
    collision_rejections = 0
    for attempt in range(attempts):
        candidate = q_seed.copy()
        if 0 < attempt < (3 * attempts) // 4:
            candidate[rig.ARM] = [
                np.clip(
                    q_seed[a] + rng.normal(0.0, 0.65),
                    rig.ARM_RANGE[k][0],
                    rig.ARM_RANGE[k][1],
                )
                for k, a in enumerate(rig.ARM)
            ]
        elif attempt:
            candidate[rig.ARM] = [
                rng.uniform(lo, hi) for lo, hi in rig.ARM_RANGE
            ]
        rig.ik_arm(
            candidate, target, top_down=False,
            approach_world=approach_target, up_world=up_target)
        eef, _ = rig.eef_pose(candidate)
        rotation = rig.data.xmat[rig.eef].reshape(3, 3)
        pos_error = float(np.linalg.norm(eef - target))
        approach_error = float(np.linalg.norm(np.cross(
            rotation @ APPROACH_LOCAL, approach_target)))
        up_error = float(np.linalg.norm(np.cross(
            rotation @ GRIPPER_UP_LOCAL, up_target)))
        if pos_error > 0.035 or approach_error > 0.25 or up_error > 0.25:
            continue
        if candidate_valid is not None and not candidate_valid(candidate):
            collision_rejections += 1
            continue
        joint_delta = np.array([
            _wrap_pi(candidate[a] - q_seed[a]) for a in rig.ARM
        ])
        joint_distance = float(np.linalg.norm(joint_delta))
        score = (
            pos_error + 0.2 * (approach_error + up_error)
            + 0.04 * joint_distance)
        if best is None or score < best[0]:
            best = (
                score, candidate.copy(), pos_error, approach_error, up_error,
                joint_distance)
    if best is None:
        raise ValueError(
            "horizontal grasp IK found no accurate collision-free candidate "
            f"in {attempts} seeds ({collision_rejections} collided)")
    _, q_best, pos_error, approach_error, up_error, joint_distance = best
    return q_best, {
        "position_error": pos_error,
        "approach_error": approach_error,
        "up_error": up_error,
        "joint_distance": joint_distance,
        "collision_rejections": collision_rejections,
    }


HORIZONTAL_GRASP_Z_OFFSET = 0.05
HORIZONTAL_GRASP_IK_ROUNDS = 3
HORIZONTAL_GRASP_IK_SEED_STRIDE = 10000
# Side-grasped objects can need a different pinch height than the conservative
# global default. Values are offsets from the common body-origin + 2 cm point.
HORIZONTAL_GRASP_Z_OFFSET_BY_OBJECT = {
    # The can is shorter than the condiment bottle. The global +5 cm side
    # pinch makes robot0's Cartesian wrist orientation miss its tolerance;
    # +2 cm clears the support while remaining feasible for both robots.
    "canned_food_1": 0.02,
}


def gen_pick(rig, q_start, obj_name, label=None, grasp_mode="top_down",
             return_to_ready=True, grasp_offset=None, post_grasp_lift=0.0,
             ready_torso=None):
    """Pick with either a vertical top-down or horizontal side approach.

    top_down (default): ready -> above -> descend -> close -> ready.
    horizontal: ready -> object-front pregrasp -> advance horizontally -> close
    -> retreat horizontally. With the experimental `return_to_ready` flag, add
    one final closed-gripper transition from pregrasp to the standard ready arm.
    """
    if grasp_mode not in {"top_down", "horizontal"}:
        raise ValueError(
            f"unknown grasp_mode '{grasp_mode}' (expected 'top_down' or 'horizontal')")
    label = label or f"pick_{obj_name}"
    ready_q = q_start.copy()
    rig.apply_ready(ready_q)
    if ready_torso is not None:
        torso = float(ready_torso)
        torso_joint = mujoco.mj_name2id(
            rig.model, mujoco.mjtObj.mjOBJ_JOINT,
            f"mobilebase{rig.robot}_joint_torso_height")
        torso_range = rig.model.jnt_range[torso_joint]
        if not np.isfinite(torso) or torso < torso_range[0] or torso > torso_range[1]:
            raise ValueError(
                f"ready_torso must be in [{torso_range[0]}, {torso_range[1]}]")
        ready_q[rig.TORSO] = torso
    rig.set_fingers(ready_q, rig.finger_open)
    # body_xy reads MuJoCo's live data, so synchronize it to this step's actual
    # threaded state. Otherwise a preceding planner's final collision probe can
    # leak a stale object pose into grasp targeting.
    rig.data.qpos[:] = ready_q
    mujoco.mj_forward(rig.model, rig.data)
    if grasp_offset is None:
        effective_grasp_offset = np.array([0.0, 0.0, 0.02])
        if grasp_mode == "horizontal":
            effective_grasp_offset[2] += HORIZONTAL_GRASP_Z_OFFSET_BY_OBJECT.get(
                obj_name, HORIZONTAL_GRASP_Z_OFFSET)
    else:
        effective_grasp_offset = np.asarray(grasp_offset, dtype=float)
        if effective_grasp_offset.shape != (3,) or not np.all(
                np.isfinite(effective_grasp_offset)):
            raise ValueError("grasp_offset must be a finite [x, y, z]")
    grasp = (
        rig.body_xy(f"{obj_name}_main") + effective_grasp_offset)
    grasp_diagnostics = None
    if grasp_mode == "top_down":
        pre = ready_q.copy()
        rig.ik_arm(pre, grasp + [0, 0, 0.12])
        at = pre.copy()
        rig.ik_arm(at, grasp)
    else:
        # Without an authored full XYZ offset, retain the historical elevated
        # horizontal target above. A manifest-provided offset replaces that
        # default entirely, so [0, 0, 0] means the object's body origin.
        # Direction from the robot/base toward the object, projected onto the
        # floor. `pre` is 12 cm back along that direction, so pre->at is a
        # genuinely horizontal advance rather than a vertical descent.
        base_xy = rig.base_xy(ready_q)
        approach = np.array([
            float(grasp[0] - base_xy[0]),
            float(grasp[1] - base_xy[1]),
            0.0,
        ])
        norm = float(np.linalg.norm(approach))
        if norm < 1e-6:
            raise ValueError(
                f"cannot infer horizontal grasp direction for '{obj_name}' "
                "(object is directly above the base)")
        approach /= norm
        pre_pos = grasp - 0.12 * approach

        arm_root = mujoco.mj_name2id(
            rig.model, mujoco.mjtObj.mjOBJ_BODY,
            f"robot{rig.robot}_link0")
        object_root = mujoco.mj_name2id(
            rig.model, mujoco.mjtObj.mjOBJ_BODY, f"{obj_name}_main")
        arm_geoms = _body_subtree_geoms(rig.model, arm_root)
        object_geoms = _body_subtree_geoms(rig.model, object_root)
        edge_adrs = [rig.TORSO, *rig.ARM, *rig.FINGERS]
        edge_res = np.array([
            RRT_TORSO_EDGE_RES,
            *([RRT_EDGE_RES] * len(rig.ARM)),
            0.01,
            0.01,
        ])

        def pose_collision_free(q, allow_object_contact=False):
            rig.data.qpos[:] = q
            mujoco.mj_forward(rig.model, rig.data)
            for contact_index in range(rig.data.ncon):
                contact = rig.data.contact[contact_index]
                g1, g2 = int(contact.geom1), int(contact.geom2)
                if g1 not in arm_geoms and g2 not in arm_geoms:
                    continue
                if (allow_object_contact
                        and ((g1 in arm_geoms and g2 in object_geoms)
                             or (g2 in arm_geoms and g1 in object_geoms))):
                    continue
                return False
            return True

        def edge_collision_free(a, b, allow_object_contact=False):
            delta = np.abs(b[edge_adrs] - a[edge_adrs])
            count = max(1, int(np.ceil(np.max(delta / edge_res))))
            return all(
                pose_collision_free(
                    (1.0 - alpha) * a + alpha * b,
                    allow_object_contact=allow_object_contact)
                for alpha in np.linspace(0.0, 1.0, count + 1)[1:])

        cartesian_count = max(
            1, int(np.ceil(np.linalg.norm(grasp - pre_pos) / 0.02)))
        round_errors = []
        for ik_round in range(HORIZONTAL_GRASP_IK_ROUNDS):
            round_seed = (
                RRT_SEED + 1000
                + ik_round * HORIZONTAL_GRASP_IK_SEED_STRIDE
            )
            try:
                pre, pre_diag = _solve_approach_ik(
                    rig, ready_q, pre_pos, approach, WORLD_UP,
                    seed=round_seed,
                    candidate_valid=lambda candidate: edge_collision_free(
                        ready_q, candidate))

                cartesian = []
                current = pre
                cartesian_errors = []
                for waypoint_index, alpha in enumerate(
                        np.linspace(0.0, 1.0, cartesian_count + 1)[1:],
                        start=1):
                    target = (1.0 - alpha) * pre_pos + alpha * grasp
                    candidate = current.copy()
                    rig.ik_arm(
                        candidate, target, top_down=False,
                        approach_world=approach, up_world=WORLD_UP)
                    eef, _ = rig.eef_pose(candidate)
                    rotation = rig.data.xmat[rig.eef].reshape(3, 3)
                    position_error = float(np.linalg.norm(eef - target))
                    approach_error = float(np.linalg.norm(np.cross(
                        rotation @ APPROACH_LOCAL, approach)))
                    up_error = float(np.linalg.norm(np.cross(
                        rotation @ GRIPPER_UP_LOCAL, WORLD_UP)))
                    if (position_error > 0.035 or approach_error > 0.25
                            or up_error > 0.25):
                        raise ValueError(
                            f"Cartesian IK failed at "
                            f"{waypoint_index}/{cartesian_count} "
                            f"(position={position_error:.3f}m, "
                            f"approach={approach_error:.3f}, "
                            f"up={up_error:.3f})")
                    if not edge_collision_free(
                            current, candidate, allow_object_contact=True):
                        raise ValueError(
                            f"Cartesian edge collides at "
                            f"{waypoint_index}/{cartesian_count}")
                    cartesian.append(candidate)
                    cartesian_errors.append(position_error)
                    current = candidate
            except ValueError as exc:
                round_errors.append(f"round {ik_round + 1}: {exc}")
                continue
            break
        else:
            raise ValueError(
                "horizontal grasp failed after "
                f"{HORIZONTAL_GRASP_IK_ROUNDS} deterministic IK rounds "
                f"({' ; '.join(round_errors)})")
        at = cartesian[-1]
        at_diag = {
            "position_error": cartesian_errors[-1],
            "approach_error": approach_error,
            "up_error": up_error,
            "cartesian_waypoints": cartesian_count,
        }
        grasp_diagnostics = {
            "pregrasp": pre_diag,
            "grasp": at_diag,
            "approach_world": [float(v) for v in approach],
            "ik_round": ik_round + 1,
            "ik_seed": round_seed,
        }
    closed = at.copy(); rig.set_fingers(closed, rig.finger_closed)  # close on the object
    if grasp_mode == "top_down":
        back = ready_q.copy()
        rig.set_fingers(back, rig.finger_closed)
    else:
        # Replay the verified Cartesian approach in reverse while carrying.
        retreat = []
        post_grasp_lift = float(post_grasp_lift)
        if post_grasp_lift < 0.0 or not np.isfinite(post_grasp_lift):
            raise ValueError("post_grasp_lift must be a finite non-negative number")
        if post_grasp_lift > 0.0:
            source_retreat = [*reversed(cartesian[:-1]), pre]
            current = closed.copy()
            source_eef, _ = rig.eef_pose(closed)
            lifted_target = source_eef + np.array([0.0, 0.0, post_grasp_lift])
            lifted = current.copy()
            rig.ik_arm(
                lifted, lifted_target, top_down=False,
                approach_world=approach, up_world=WORLD_UP)
            rig.set_fingers(lifted, rig.finger_closed)
            retreat.append(lifted)
            current = lifted
            for source in source_retreat:
                source_target, _ = rig.eef_pose(source)
                target = source_target + np.array([0.0, 0.0, post_grasp_lift])
                candidate = current.copy()
                rig.ik_arm(
                    candidate, target, top_down=False,
                    approach_world=approach, up_world=WORLD_UP)
                rig.set_fingers(candidate, rig.finger_closed)
                retreat.append(candidate)
                current = candidate
            pre_back = retreat[-1]
        else:
            for waypoint in reversed(cartesian[:-1]):
                waypoint = waypoint.copy()
                rig.set_fingers(waypoint, rig.finger_closed)
                retreat.append(waypoint)
            pre_back = pre.copy()
            rig.set_fingers(pre_back, rig.finger_closed)
            retreat.append(pre_back)
        if return_to_ready:
            back = ready_q.copy()
            rig.set_fingers(back, rig.finger_closed)
        else:
            back = pre_back
    off_pos, off_quat = _grasp_offset(rig, obj_name, at)
    # Top-down keeps the historical five waypoints. Horizontal expands the
    # approach and retreat so playback follows the collision-checked Cartesian
    # edges rather than interpolating directly between distant IK endpoints.
    if grasp_mode == "top_down":
        pick_waypoints = [ready_q, pre, at, closed, back]
        pick_durations = [1.2, 0.8, 0.4, 1.2]
        attach_from_seg = 3
    else:
        approach_duration = 0.8 / len(cartesian)
        retreat_duration = 1.2 / len(retreat)
        pick_waypoints = [ready_q, pre, *cartesian, closed, *retreat]
        pick_durations = [
            1.2,
            *([approach_duration] * len(cartesian)),
            0.4,
            *([retreat_duration] * len(retreat)),
        ]
        if return_to_ready:
            pick_waypoints.append(back)
            pick_durations.append(1.2)
        attach_from_seg = 2 + len(cartesian)

    obj = {
        "name": obj_name, "attach_from_seg": attach_from_seg,
        "off_pos": off_pos, "off_quat": off_quat,
        "static_pose": _rest_obj_pose(rig, obj_name),
    }
    track = build_track(
        rig, label, pick_waypoints, pick_durations, obj)
    track["meta"]["grasp_mode"] = grasp_mode
    track["meta"]["grasp_offset"] = [
        float(value) for value in effective_grasp_offset]
    track["meta"]["post_grasp_lift"] = float(post_grasp_lift)
    track["meta"]["return_to_ready"] = bool(
        grasp_mode == "horizontal" and return_to_ready)
    if grasp_mode == "horizontal":
        track["meta"]["horizontal_pregrasp_arm_seed"] = [
            float(pre[a]) for a in rig.ARM
        ]
    if grasp_diagnostics is not None:
        track["meta"]["grasp_diagnostics"] = grasp_diagnostics
    return track, back, (off_pos, off_quat)


def _upright_release_pose(raw_pose, rest_quat):
    """Remove release roll/pitch while preserving the motion-selected yaw."""
    raw_pos, raw_quat = raw_pose
    desired_up = quat_rot(rest_quat, np.array([0.0, 0.0, 1.0]))
    raw_up = quat_rot(raw_quat, np.array([0.0, 0.0, 1.0]))
    correction = quat_align_vectors(raw_up, desired_up)
    return raw_pos, quat_mul(correction, raw_quat)


def gen_place(rig, q_start, obj_name, dest_xyz, off, label=None):
    """Arm: ready(holding) -> above dest -> down -> open -> back to ready.
    Base assumed already at the destination standoff."""
    label = label or f"place_{obj_name}"
    off_pos, off_quat = off
    dest = np.asarray(dest_xyz)
    # Preserve only the placed object's vertical direction. Horizontal picks
    # can leave an upright carton sideways when the gripper transitions into the
    # historical top-down place frame. At release, remove that roll/pitch while
    # retaining the unconstrained yaw chosen by the motion.
    rest_quat = _rest_obj_pose(rig, obj_name)[1]
    desired_obj_up = quat_rot(rest_quat, np.array([0.0, 0.0, 1.0]))

    # `dest` z is the surface height; land the object's base on it by putting its
    # origin `h` above (h = how high it sat above its support at rest). We IK the
    # gripper (not the object), so first aim high, then correct the target z by
    # the residual between where the carried object actually lands (gripper pose +
    # grasp offset) and where we want it.
    h = _rest_support_clearance(rig, obj_name)
    target_obj_z = float(dest[2]) + h
    xy = np.array([float(dest[0]), float(dest[1]), 0.0])
    start = q_start.copy()                         # arm ready, holding (fingers closed)
    rig.set_fingers(start, rig.finger_closed)
    pre = start.copy(); rig.ik_arm(pre, xy + [0, 0, target_obj_z + 0.14])
    tgt_z = target_obj_z + 0.02
    drop = pre.copy(); rig.ik_arm(drop, xy + [0, 0, tgt_z])
    tgt_z += target_obj_z - float(_carried_pose(rig, drop, off_pos, off_quat)[0][2])
    drop = pre.copy(); rig.ik_arm(drop, xy + [0, 0, tgt_z])
    opened = drop.copy(); rig.set_fingers(opened, rig.finger_open)  # release
    back = drop.copy(); rig.apply_ready(back); rig.set_fingers(back, rig.finger_open)
    # segments: start->pre->drop->opened->back. Held through the drop (segs 0..2);
    # released at seg 3 (back) and left where it was dropped.
    raw_released_pose = _carried_pose(rig, opened, off_pos, off_quat)
    released_pose = _upright_release_pose(raw_released_pose, rest_quat)
    obj = {
        "name": obj_name, "attach_from_seg": 0, "detach_from_seg": 3,
        "off_pos": off_pos, "off_quat": off_quat, "static_pose": released_pose,
        "orient_to_static_from_seg": 2,
    }
    track = build_track(rig, label,
                        [start, pre, drop, opened, back], [1.2, 0.6, 0.3, 1.2], obj)
    released_up = quat_rot(released_pose[1], np.array([0.0, 0.0, 1.0]))
    track["meta"]["object_up_alignment"] = float(
        np.dot(released_up, desired_obj_up)
        / (np.linalg.norm(released_up) * np.linalg.norm(desired_obj_up)))
    return track, back


def _body_subtree_geoms(model, root_body):
    """Collision-capable geoms on `root_body` and all of its descendants."""
    bodies = set()
    for body in range(model.nbody):
        cur = body
        while cur > 0 and cur != root_body:
            cur = int(model.body_parentid[cur])
        if cur == root_body:
            bodies.add(body)
    return {
        g for g in range(model.ngeom)
        if int(model.geom_bodyid[g]) in bodies
        and (int(model.geom_contype[g]) or int(model.geom_conaffinity[g]))
    }


def _robot_collision_checker(rig, q_reference):
    """Return a whole-robot validity predicate for local reset/base motions.

    Contacts already present at the segment's starting pose are treated as the
    support baseline (normally wheels/base against the floor). Any new contact
    between this robot and the rest of the scene invalidates the pose. Robot
    self-contacts are ignored here because these local motions start from and
    remain close to an already accepted robot configuration.
    """
    m, d = rig.model, rig.data
    robot_root = rig.base_body
    while int(m.body_parentid[robot_root]) > 0:
        robot_root = int(m.body_parentid[robot_root])
    robot_geoms = _body_subtree_geoms(m, robot_root)

    def external_pairs(q):
        d.qpos[:] = q
        mujoco.mj_forward(m, d)
        pairs = set()
        for contact_index in range(d.ncon):
            contact = d.contact[contact_index]
            g1, g2 = int(contact.geom1), int(contact.geom2)
            in1, in2 = g1 in robot_geoms, g2 in robot_geoms
            if in1 == in2:
                continue
            pairs.add((g1, g2) if in1 else (g2, g1))
        return pairs

    allowed_pairs = external_pairs(q_reference)

    def valid(q):
        return external_pairs(q).issubset(allowed_pairs)

    return valid


def _qpos_edge_collision_free(a, b, adrs, resolution, valid):
    """Collision-check a qpos edge at per-coordinate interpolation resolution."""
    adrs = np.asarray(adrs, dtype=int)
    resolution = np.broadcast_to(
        np.asarray(resolution, dtype=float), (len(adrs),))
    delta = np.abs(b[adrs] - a[adrs])
    count = max(1, int(np.ceil(np.max(delta / resolution))))
    for alpha in np.linspace(0.0, 1.0, count + 1)[1:]:
        q = a.copy()
        q[adrs] = (1.0 - alpha) * a[adrs] + alpha * b[adrs]
        if not valid(q):
            return False
    return True


def _container_scene_state(rig, q, interior_body, state_source="study_init"):
    """Return planning state and the containing fixture root for a reach-in.

    ``study_init`` restores the fixture state baked into the scene keyframe.
    ``current`` is for an articulated container whose Open replay has just
    written a newer door/drawer state into ``q``. Keeping that qpos is
    essential when the current state differs from the scene keyframe.
    """
    m = rig.model
    interior = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, interior_body)
    if interior < 0:
        raise ValueError(f"unknown interior_body '{interior_body}'")
    root = int(m.body_parentid[interior])
    if root <= 0:
        raise ValueError(f"interior_body '{interior_body}' has no cabinet parent")
    if state_source == "current":
        return q.copy(), root
    if state_source != "study_init":
        raise ValueError(
            f"unknown front-place state_source '{state_source}' "
            "(expected 'study_init' or 'current')")
    key = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_KEY, "study_init")
    if key < 0:
        key = 0 if m.nkey else -1
    if key < 0:
        raise ValueError("front-access placement requires a scene init keyframe")

    out = q.copy()
    root_bodies = set()
    for body in range(m.nbody):
        cur = body
        while cur > 0 and cur != root:
            cur = int(m.body_parentid[cur])
        if cur == root:
            root_bodies.add(body)
    for jid in range(m.njnt):
        if int(m.jnt_bodyid[jid]) not in root_bodies:
            continue
        adr = int(m.jnt_qposadr[jid])
        width = 7 if m.jnt_type[jid] == mujoco.mjtJoint.mjJNT_FREE else (
            4 if m.jnt_type[jid] == mujoco.mjtJoint.mjJNT_BALL else 1)
        out[adr:adr + width] = m.key_qpos[key, adr:adr + width]
    return out, root


def _rrt_connect(start, goal, valid, limits, rng, edge_res=RRT_EDGE_RES,
                 step_scale=None, sample_res=None):
    """Deterministic bidirectional RRT-Connect in scaled joint space."""
    step_scale = np.asarray(
        step_scale if step_scale is not None
        else np.full(len(start), RRT_STEP), dtype=float)
    edge_res = np.broadcast_to(
        np.asarray(edge_res, dtype=float), (len(start),))
    if np.any(step_scale <= 0.0) or np.any(edge_res <= 0.0):
        raise ValueError("RRT step and edge resolutions must be positive")

    sample_levels = []
    for index, (lo, hi) in enumerate(limits):
        resolution = None if sample_res is None else sample_res[index]
        if resolution is None:
            sample_levels.append(None)
            continue
        values = lo + float(resolution) * np.arange(
            int(np.floor((hi - lo) / float(resolution))) + 1)
        if values.size == 0 or hi - values[-1] > 1e-9:
            values = np.append(values, hi)
        sample_levels.append(values)

    def edge_valid(a, b):
        n = max(1, int(np.ceil(np.max(np.abs(b - a) / edge_res))))
        return all(valid((1.0 - t) * a + t * b) for t in np.linspace(0.0, 1.0, n + 1)[1:])

    def tree(root, kind):
        return {"q": [root.copy()], "parent": [-1], "kind": kind}

    def nearest(t, q):
        return min(
            range(len(t["q"])),
            key=lambda i: float(np.linalg.norm((t["q"][i] - q) / step_scale)))

    def extend(t, target):
        near = nearest(t, target)
        delta = target - t["q"][near]
        dist = float(np.linalg.norm(delta / step_scale))
        new = target.copy() if dist <= 1.0 else t["q"][near] + delta / dist
        if not edge_valid(t["q"][near], new):
            return "trapped", near
        t["q"].append(new)
        t["parent"].append(near)
        return ("reached" if dist <= 1.0 else "advanced"), len(t["q"]) - 1

    def connect(t, target):
        while True:
            status, idx = extend(t, target)
            if status != "advanced":
                return status, idx

    def trace(t, idx):
        out = []
        while idx >= 0:
            out.append(t["q"][idx])
            idx = t["parent"][idx]
        return list(reversed(out))

    ta, tb = tree(start, "start"), tree(goal, "goal")
    for iteration in range(RRT_MAX_ITERS):
        if rng.random() < 0.15:
            sample = goal
        else:
            sample = np.array([
                rng.uniform(lo, hi) if levels is None else rng.choice(levels)
                for (lo, hi), levels in zip(limits, sample_levels)
            ], dtype=float)
        status_a, ia = extend(ta, sample)
        if status_a != "trapped":
            status_b, ib = connect(tb, ta["q"][ia])
            if status_b == "reached":
                pa, pb = trace(ta, ia), trace(tb, ib)
                if ta["kind"] == "start":
                    start_side, goal_side = pa, pb
                else:
                    start_side, goal_side = pb, pa
                return start_side + list(reversed(goal_side[:-1])), iteration + 1, edge_valid
        ta, tb = tb, ta
    raise ValueError(f"RRT-Connect failed after {RRT_MAX_ITERS} iterations")


def _shortcut_path(path, edge_valid, rng):
    path = [q.copy() for q in path]
    for _ in range(RRT_SHORTCUT_ITERS):
        if len(path) <= 2:
            break
        i, j = sorted(rng.integers(0, len(path), size=2))
        if j <= i + 1:
            continue
        if edge_valid(path[i], path[j]):
            path = path[:i + 1] + path[j:]
    return path


def gen_place_reachin(rig, q_start, obj_name, interior_body, off, label=None,
                      dest_xyz=None, grasp_mode="top_down", goal_arm_seed=None,
                      state_source="study_init", support_geom=None,
                      front_distance=None, ik_eef_tolerance=0.012,
                      randomize_preinsert_first=False,
                      ik_top_down=False, rrt_horizontal_ingress=False,
                      reachin_lift_height=None, simple_ingress=True,
                      release_clearance=0.01,
                      cartesian_rrt_fallback=False,
                      nearby_rrt_fallback=False,
                      validate_open_gripper=False,
                      release_fingers=None,
                      return_planning_details=False):
    """Collision-checked horizontal reach-in place into an open container.

    The base remains fixed. RRT-Connect first reaches a collision-free high pose
    in front of the opening. Dense Cartesian-IK waypoints then move the held
    object horizontally above the shelf target and vertically down to it. Both
    the arm and the kinematically-held object participate in cabinet collision
    checking. The complete route is replayed in reverse after release so the
    gripper withdraws along an already-validated corridor.
    """
    planner_started = time.perf_counter()
    label = label or f"place_{obj_name}"
    release_fingers = (
        rig.finger_open if release_fingers is None
        else [float(value) for value in release_fingers])
    if len(release_fingers) != 2:
        raise ValueError("release_fingers must contain two joint values")
    rng = np.random.default_rng(RRT_SEED + rig.robot)
    off_pos, off_quat = off
    scene_q, container_root = _container_scene_state(
        rig, q_start, interior_body, state_source=state_source)
    rig.set_fingers(scene_q, rig.finger_closed)
    horizontal_approach = horizontal_up = None
    if grasp_mode in {"horizontal", "ready"}:
        orientation_q = scene_q.copy()
        if goal_arm_seed is not None:
            orientation_q[rig.ARM] = goal_arm_seed
        rig.eef_pose(orientation_q)
        start_rotation = rig.data.xmat[rig.eef].reshape(3, 3).copy()
        horizontal_approach = start_rotation @ APPROACH_LOCAL
        horizontal_up = start_rotation @ GRIPPER_UP_LOCAL

    if dest_xyz is None:
        dest_xyz = _container_dest_point(
            rig, interior_body, scene_q, 0, 1, None,
            support_geom=support_geom, front_access=True)
    dest = np.asarray(dest_xyz, dtype=float)
    desired_obj = dest.copy()
    # A little free space is intentional: the held bottle tilts during the
    # position-only shelf IK, and validity checking (rather than an assumed
    # upright clearance) decides whether its full collision mesh fits.
    desired_obj[2] += (
        _rest_support_clearance(rig, obj_name) + float(release_clearance))
    # Enter normal to the shelf opening.  A robot standoff can be slightly
    # off-centre, so base-dest points diagonally through narrow French doors
    # and can make link6 scrape a side frame even when both endpoint IK poses
    # are clear.  Use the same shelf-local outward axis as destination
    # placement so the complete motion shares one scene-derived frame.
    front_dir = np.array([
        *_container_front_direction(
            rig, interior_body, scene_q, support_geom=support_geom),
        0.0,
    ])
    reachin_lift_height = (
        REACHIN_LIFT_HEIGHT if reachin_lift_height is None
        else float(reachin_lift_height))
    high_offset = np.array([0.0, 0.0, reachin_lift_height])
    reachin_front_distance = (
        REACHIN_FRONT_DISTANCE if front_distance is None else float(front_distance))
    front_high_obj = desired_obj + reachin_front_distance * front_dir + high_offset
    inside_high_obj = desired_obj + high_offset

    m, d = rig.model, rig.data
    arm_root = mujoco.mj_name2id(
        m, mujoco.mjtObj.mjOBJ_BODY, f"robot{rig.robot}_link0")
    obj_root = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, f"{obj_name}_main")
    arm_geoms = _body_subtree_geoms(m, arm_root)
    obj_geoms = _body_subtree_geoms(m, obj_root)
    container_geoms = _body_subtree_geoms(m, container_root)
    moving_geoms = arm_geoms | obj_geoms
    obj_adr = rig.jadr(_obj_joint(obj_name))
    stats = {"checks": 0, "rejected": 0, "collision_time_sec": 0.0}

    torso_jid = mujoco.mj_name2id(
        m, mujoco.mjtObj.mjOBJ_JOINT,
        f"mobilebase{rig.robot}_joint_torso_height")
    plan_adrs = [rig.TORSO, *rig.ARM]
    torso_range = tuple(map(float, m.jnt_range[torso_jid]))
    limits = [torso_range]
    for k, value in enumerate(scene_q[rig.ARM]):
        if rig.ARM_LIMITED[k]:
            limits.append(tuple(map(float, rig.ARM_RANGE[k])))
        else:
            limits.append((float(value - np.pi), float(value + np.pi)))

    def q_for_config(config):
        q = scene_q.copy()
        q[plan_adrs] = config
        rig.set_fingers(q, rig.finger_closed)
        op, oq = _carried_pose(rig, q, off_pos, off_quat)
        q[obj_adr:obj_adr + 7] = [*op, *oq]
        return q

    # Contact pairs already present at the start pose are baseline, not
    # obstacles: a replayed Open* demo can end with the gripper still resting
    # on the handle it pulled, or the carried object grazing the fixture.
    # Motion away from the start immediately separates them; only NEW
    # moving<->container contact pairs reject a configuration.
    baseline_pairs: set[tuple[int, int]] = set()

    def valid(config):
        check_started = time.perf_counter()
        try:
            stats["checks"] += 1
            for value, (lo, hi) in zip(config, limits):
                if value < lo - 1e-6 or value > hi + 1e-6:
                    stats["rejected"] += 1
                    stats["last_rejection"] = "joint_limit"
                    return False
            q = q_for_config(config)
            finger_states = [rig.finger_closed]
            if validate_open_gripper:
                finger_states = [
                    [
                        (1.0 - alpha) * closed + alpha * opened
                        for closed, opened in zip(
                            rig.finger_closed, release_fingers)
                    ]
                    for alpha in np.linspace(0.0, 1.0, 5)
                ]
            for finger_index, finger_state in enumerate(finger_states):
                rig.set_fingers(q, finger_state)
                d.qpos[:] = q
                mujoco.mj_forward(m, d)
                for ci in range(d.ncon):
                    contact = d.contact[ci]
                    g1, g2 = int(contact.geom1), int(contact.geom2)
                    if ((g1 in moving_geoms and g2 in container_geoms)
                            or (g2 in moving_geoms and g1 in container_geoms)):
                        if (min(g1, g2), max(g1, g2)) in baseline_pairs:
                            continue
                        stats["rejected"] += 1
                        stats["last_collision"] = (
                            mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, g1),
                            mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, g2),
                        )
                        stats["last_rejection"] = (
                            "open_gripper_contact"
                            if finger_index else "contact")
                        return False
            return True
        finally:
            stats["collision_time_sec"] += time.perf_counter() - check_started

    def _contact_pairs(config):
        q = q_for_config(config)
        d.qpos[:] = q
        mujoco.mj_forward(m, d)
        pairs = set()
        for ci in range(d.ncon):
            contact = d.contact[ci]
            g1, g2 = int(contact.geom1), int(contact.geom2)
            if ((g1 in moving_geoms and g2 in container_geoms)
                    or (g2 in moving_geoms and g1 in container_geoms)):
                pairs.add((min(g1, g2), max(g1, g2)))
        return pairs

    start_config = scene_q[plan_adrs].copy()
    # A recorded replay can end epsilon-outside a hard joint limit (e.g. the
    # 012 OpenCabinet demo leaves the torso at -2e-5 for a [0, 0.34] range).
    # Clamp numerical noise only; a real violation still fails below.
    for k, (lo, hi) in enumerate(limits):
        v = float(start_config[k])
        if lo - 1e-3 <= v < lo:
            start_config[k] = lo
        elif hi < v <= hi + 1e-3:
            start_config[k] = hi
    baseline_pairs |= _contact_pairs(start_config)
    if baseline_pairs:
        stats["baseline_contacts"] = sorted(
            (mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, a),
             mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, b))
            for a, b in baseline_pairs)
    if not valid(start_config):
        raise ValueError(
            "front-place start pose is invalid: "
            f"reason={stats.get('last_rejection')} "
            f"contact={stats.get('last_collision')}")

    def solve_carried_target(candidate, target_obj):
        """Move a held object's origin to target_obj while preserving grip frame."""
        candidate = candidate.copy()
        rig.set_fingers(candidate, rig.finger_closed)
        ep, _ = rig.eef_pose(candidate)
        carried, _ = _carried_pose(rig, candidate, off_pos, off_quat)
        eef_target = ep + (target_obj - carried)
        for _ in range(4):
            rig.ik_arm(
                candidate,
                eef_target,
                top_down=ik_top_down,
                approach_world=None if ik_top_down else horizontal_approach,
                up_world=None if ik_top_down else horizontal_up,
            )
            ep, _ = rig.eef_pose(candidate)
            carried, _ = _carried_pose(rig, candidate, off_pos, off_quat)
            residual = target_obj - carried
            if np.linalg.norm(residual) <= 0.008:
                break
            eef_target += np.clip(residual, -0.04, 0.04)
        ep, _ = rig.eef_pose(candidate)
        carried, _ = _carried_pose(rig, candidate, off_pos, off_quat)
        return (
            candidate,
            eef_target,
            float(np.linalg.norm(ep - eef_target)),
            float(np.linalg.norm(carried - target_obj)),
        )

    preinsert_q = None
    preinsert_eef_target = None
    preinsert_eef_error = preinsert_obj_error = float("inf")
    for attempt in range(48):
        candidate = scene_q.copy()
        candidate[rig.TORSO] = (
            torso_range[1] if attempt == 0 else rng.uniform(*torso_range))
        if attempt == 0 and goal_arm_seed is not None:
            candidate[rig.ARM] = goal_arm_seed
        if attempt or randomize_preinsert_first:
            candidate[rig.ARM] = [
                rng.uniform(lo, hi) for lo, hi in limits[1:]
            ]
        candidate, eef_target, eef_error, obj_error = solve_carried_target(
            candidate, front_high_obj)
        if (eef_error <= ik_eef_tolerance and obj_error <= 0.015
                and valid(candidate[plan_adrs])):
            preinsert_q = candidate
            preinsert_eef_target = eef_target.copy()
            preinsert_eef_error, preinsert_obj_error = eef_error, obj_error
            break
    if preinsert_q is None:
        raise ValueError(
            "no collision-free container pre-insert IK solution in 48 deterministic seeds")

    preinsert_config = preinsert_q[plan_adrs].copy()

    checks_before_rrt = stats["checks"]
    collision_time_before_rrt = stats["collision_time_sec"]
    rrt_step_scale = np.array(
        [RRT_TORSO_STEP, *([RRT_STEP] * len(rig.ARM))])
    rrt_edge_res = np.array(
        [RRT_TORSO_EDGE_RES, *([RRT_EDGE_RES] * len(rig.ARM))])
    rrt_sample_res = [RRT_TORSO_SAMPLE_RES, *([None] * len(rig.ARM))]

    def fine_edge_valid(a, b):
        validation_res = np.array([
            RRT_TORSO_EDGE_RES,
            *([RRT_VALIDATION_EDGE_RES] * len(rig.ARM)),
        ])
        n = max(1, int(np.ceil(
            np.max(np.abs(b - a) / validation_res))))
        return all(
            valid((1.0 - t) * a + t * b)
            for t in np.linspace(0.0, 1.0, n + 1)[1:])

    # Fast path: try a direct joint-space interpolation from the start pose to
    # the pre-insert pose (like gen_pick's ready->pre transition) before paying
    # for the full RRT-Connect search. The base is fixed and both endpoints are
    # in the open, so this straight line is usually collision-free; only fall
    # back to sampling-based planning when the dense validity check finds it
    # scrapes the container.
    rrt_started = time.perf_counter()
    ingress_planner = "straight_ik"
    rrt_iterations = 0
    if simple_ingress and fine_edge_valid(start_config, preinsert_config):
        raw = [start_config, preinsert_config]
    else:
        if simple_ingress:
            ingress_planner = "rrt_connect_fallback"
        else:
            ingress_planner = "rrt_connect"
        raw, rrt_iterations, _ = _rrt_connect(
            start_config, preinsert_config, valid, limits, rng,
            edge_res=rrt_edge_res,
            step_scale=rrt_step_scale,
            sample_res=rrt_sample_res)
    rrt_elapsed = time.perf_counter() - rrt_started
    rrt_search_checks = stats["checks"] - checks_before_rrt
    rrt_collision_time = (
        stats["collision_time_sec"] - collision_time_before_rrt)
    rrt_tree_time = max(0.0, rrt_elapsed - rrt_collision_time)

    smooth = _shortcut_path(raw, fine_edge_valid, rng)
    rrt_collision_free = all(
        fine_edge_valid(a, b) for a, b in zip(smooth, smooth[1:]))
    if not rrt_collision_free:
        raise AssertionError(
            "shortcut produced a colliding reach-in path; "
            f"ingress_planner={ingress_planner}; "
            f"rrt_iterations={rrt_iterations}; "
            f"rrt_search_checks={rrt_search_checks}")

    forward = [q_for_config(config) for config in smooth]
    cartesian_start_checks = stats["checks"]
    cartesian_counts = []
    cartesian_errors = []

    def append_cartesian_segment(target_obj, segment_name):
        start_obj, _ = _carried_pose(
            rig, forward[-1], off_pos, off_quat)
        distance = float(np.linalg.norm(target_obj - start_obj))
        count = max(1, int(np.ceil(distance / REACHIN_CARTESIAN_STEP)))
        previous_config = forward[-1][plan_adrs].copy()
        for waypoint_index, alpha in enumerate(
                np.linspace(0.0, 1.0, count + 1)[1:], start=1):
            waypoint_obj = (1.0 - alpha) * start_obj + alpha * target_obj
            candidate, _, eef_error, obj_error = solve_carried_target(
                forward[-1], waypoint_obj)
            config = candidate[plan_adrs].copy()
            if eef_error > ik_eef_tolerance or obj_error > 0.015:
                raise ValueError(
                    f"{segment_name} Cartesian IK residual too large "
                    f"(eef={eef_error:.4f}, object={obj_error:.4f})")
            point_valid = valid(config)
            edge_is_valid = point_valid and fine_edge_valid(
                previous_config, config)
            if not edge_is_valid:
                raise ValueError(
                    f"{segment_name} Cartesian waypoint {waypoint_index}/{count} "
                    f"collides with container at object target "
                    f"{waypoint_obj.tolist()}; reason={stats.get('last_rejection')}; "
                    f"contact={stats.get('last_collision')}; "
                    f"rrt_iterations={rrt_iterations}; "
                    f"rrt_search_checks={rrt_search_checks}")
            forward.append(q_for_config(config))
            previous_config = config
        final_obj, _ = _carried_pose(
            rig, forward[-1], off_pos, off_quat)
        cartesian_counts.append(count)
        cartesian_errors.append(float(np.linalg.norm(final_obj - target_obj)))

    horizontal_ingress_planner = "cartesian"
    horizontal_fallback_reason = None

    def append_rrt_segment(target_obj, goal_description,
                           seed_from_current=False):
        nonlocal rrt_iterations
        # A straight EEF path can scrape a link on a door, frame, or shelf even
        # though both endpoint poses are clear. Find a collision-free endpoint
        # IK and use global joint-space validity to reach it. Horizontal ingress
        # uses this through narrow French doors; vertical lowering uses it as a
        # fallback when the local IK follows a bad redundant-arm branch.
        segment_goal = None
        for attempt in range(48):
            if seed_from_current and nearby_rrt_fallback and attempt < 24:
                candidate = forward[-1].copy()
                if attempt:
                    candidate[rig.TORSO] = np.clip(
                        candidate[rig.TORSO] + rng.uniform(-0.05, 0.05),
                        *torso_range)
                    candidate[rig.ARM] = [
                        np.clip(value + rng.normal(0.0, 0.25), lo, hi)
                        for value, (lo, hi) in zip(
                            candidate[rig.ARM], limits[1:])
                    ]
            else:
                candidate = (
                    forward[-1].copy()
                    if attempt == 0 and seed_from_current
                    else scene_q.copy()
                )
                candidate[rig.TORSO] = (
                    candidate[rig.TORSO]
                    if attempt == 0 and seed_from_current
                    else torso_range[1] if attempt == 0
                    else rng.uniform(*torso_range))
                if not (attempt == 0 and seed_from_current):
                    candidate[rig.ARM] = [
                        rng.uniform(lo, hi) for lo, hi in limits[1:]]
            candidate, _, eef_error, obj_error = solve_carried_target(
                candidate, target_obj)
            if (eef_error <= ik_eef_tolerance and obj_error <= 0.015
                    and valid(candidate[plan_adrs])):
                segment_goal = candidate[plan_adrs].copy()
                break
        if segment_goal is None:
            raise ValueError(
                f"no collision-free {goal_description} IK solution")
        raw_inside, inside_iterations, _ = _rrt_connect(
            forward[-1][plan_adrs], segment_goal, valid, limits, rng,
            edge_res=rrt_edge_res, step_scale=rrt_step_scale,
            sample_res=rrt_sample_res)
        smooth_inside = _shortcut_path(raw_inside, fine_edge_valid, rng)
        if not all(fine_edge_valid(a, b) for a, b in zip(smooth_inside, smooth_inside[1:])):
            raise AssertionError(f"{goal_description} RRT shortcut is colliding")
        forward.extend(q_for_config(config) for config in smooth_inside[1:])
        rrt_iterations += inside_iterations
        cartesian_counts.append(0)
        final_obj, _ = _carried_pose(rig, forward[-1], off_pos, off_quat)
        cartesian_errors.append(float(np.linalg.norm(final_obj - target_obj)))

    if rrt_horizontal_ingress:
        horizontal_ingress_planner = "rrt"
        append_rrt_segment(inside_high_obj, "fridge interior-high")
    else:
        horizontal_start_len = len(forward)
        try:
            append_cartesian_segment(inside_high_obj, "horizontal-insert")
        except ValueError as exc:
            if not cartesian_rrt_fallback:
                raise
            horizontal_ingress_planner = "rrt_fallback"
            horizontal_fallback_reason = str(exc)
            if nearby_rrt_fallback:
                # Keep the collision-free Cartesian prefix and plan only the
                # short remainder from its last valid waypoint. Discarding it
                # makes deep-but-reachable cabinet targets restart a global
                # RRT at the opening and multiplies collision checks.
                append_rrt_segment(
                    inside_high_obj, "container interior-high",
                    seed_from_current=True)
            else:
                del forward[horizontal_start_len:]
                append_rrt_segment(inside_high_obj, "fridge interior-high")

    vertical_lower_planner = "cartesian"
    vertical_fallback_reason = None
    vertical_start_len = len(forward)
    try:
        append_cartesian_segment(desired_obj, "vertical-lower")
    except ValueError as exc:
        if not cartesian_rrt_fallback:
            raise
        # Discard any successful Cartesian prefix before finding one clean,
        # collision-checked joint-space route from the interior-high pose to
        # the final release pose. This is especially important for a second
        # French-door-fridge placement: its lateral slot can make local IK
        # flip the redundant wrist/elbow branch into the shelf on waypoint 1.
        del forward[vertical_start_len:]
        vertical_lower_planner = "rrt_fallback"
        vertical_fallback_reason = str(exc)
        append_rrt_segment(
            desired_obj, "container final-placement", seed_from_current=True)
    cartesian_checks = stats["checks"] - cartesian_start_checks
    all_configs = [q[plan_adrs] for q in forward]
    collision_free = all(
        fine_edge_valid(a, b) for a, b in zip(all_configs, all_configs[1:]))
    if not collision_free:
        raise AssertionError("RRT + Cartesian reach-in path is not collision-free")

    final_eef, _ = rig.eef_pose(forward[-1])
    final_obj, _ = _carried_pose(
        rig, forward[-1], off_pos, off_quat)
    goal_eef_target = final_eef.copy()
    goal_eef_error = 0.0
    goal_obj_error = float(np.linalg.norm(final_obj - desired_obj))
    opened = forward[-1].copy()
    rig.set_fingers(opened, release_fingers)
    retreat = []
    for q in reversed(forward[:-1]):
        q = q.copy()
        rig.set_fingers(q, rig.finger_open)
        retreat.append(q)
    waypoints = forward + [opened] + retreat
    durations = [
        max(0.15, float(np.max(np.abs(b[plan_adrs] - a[plan_adrs]))) / RRT_ARM_SPEED)
        for a, b in zip(waypoints, waypoints[1:])
    ]
    raw_released_pose = _carried_pose(rig, opened, off_pos, off_quat)
    rest_quat = _rest_obj_pose(rig, obj_name)[1]
    desired_obj_up = quat_rot(rest_quat, np.array([0.0, 0.0, 1.0]))
    released_pose = _upright_release_pose(raw_released_pose, rest_quat)
    obj = {
        "name": obj_name,
        "attach_from_seg": 0,
        "detach_from_seg": len(forward),
        "off_pos": off_pos,
        "off_quat": off_quat,
        "static_pose": released_pose,
        "orient_to_static_from_seg": len(forward) - 1,
    }
    track = build_track(rig, label, waypoints, durations, obj)
    released_up = quat_rot(released_pose[1], np.array([0.0, 0.0, 1.0]))
    track["meta"]["object_up_alignment"] = float(
        np.dot(released_up, desired_obj_up)
        / (np.linalg.norm(released_up) * np.linalg.norm(desired_obj_up)))
    planner_elapsed = time.perf_counter() - planner_started
    track["meta"].update({
        "planner": "rrt_connect+cartesian_ik",
        "ingress_planner": ingress_planner,
        "horizontal_ingress_planner": horizontal_ingress_planner,
        "horizontal_fallback_reason": horizontal_fallback_reason,
        "vertical_lower_planner": vertical_lower_planner,
        "vertical_fallback_reason": vertical_fallback_reason,
        "rrt_seed": RRT_SEED + rig.robot,
        "rrt_iterations": rrt_iterations,
        "rrt_raw_waypoints": len(raw),
        "rrt_smoothed_waypoints": len(smooth),
        "rrt_search_collision_checks": rrt_search_checks,
        "rrt_dimensions": len(plan_adrs),
        "rrt_step_scale": [float(v) for v in rrt_step_scale],
        "rrt_torso_sample_res": RRT_TORSO_SAMPLE_RES,
        "rrt_search_edge_res": [float(v) for v in rrt_edge_res],
        "rrt_validation_edge_res": [
            RRT_TORSO_EDGE_RES,
            *([RRT_VALIDATION_EDGE_RES] * len(rig.ARM)),
        ],
        "rrt_search_time_sec": round(rrt_elapsed, 6),
        "rrt_collision_time_sec": round(rrt_collision_time, 6),
        "rrt_tree_time_sec": round(rrt_tree_time, 6),
        "rrt_iteration_time_ms": (
            round(1000.0 * rrt_tree_time / rrt_iterations, 6)
            if rrt_iterations else 0.0),
        "planner_total_time_sec": round(planner_elapsed, 6),
        "planner_collision_time_sec": round(
            stats["collision_time_sec"], 6),
        "rrt_joint_path_length": round(float(sum(
            np.linalg.norm(b - a) for a, b in zip(smooth, smooth[1:]))), 6),
        "rrt_collision_checks": stats["checks"],
        "rrt_rejected_states": stats["rejected"],
        "rrt_path_collision_free": collision_free,
        "preinsert_object_target": [float(v) for v in front_high_obj],
        "preinsert_eef_target": [float(v) for v in preinsert_eef_target],
        "preinsert_eef_distance": preinsert_eef_error,
        "preinsert_object_distance": preinsert_obj_error,
        "cartesian_horizontal_waypoints": cartesian_counts[0],
        "cartesian_vertical_waypoints": cartesian_counts[1],
        "cartesian_collision_checks": cartesian_checks,
        "cartesian_horizontal_object_error": cartesian_errors[0],
        "cartesian_vertical_object_error": cartesian_errors[1],
        "reachin_front_distance": reachin_front_distance,
        "reachin_lift_height": reachin_lift_height,
        "rrt_release_time": round(float(sum(durations[:len(forward)])), 6),
        "target_xyz": [float(v) for v in dest],
        "goal_eef_target": [float(v) for v in goal_eef_target],
        "goal_eef_distance": goal_eef_error,
        "goal_object_distance": goal_obj_error,
        "grasp_mode": grasp_mode,
        "ik_eef_tolerance": ik_eef_tolerance,
        "ik_top_down": ik_top_down,
        "rrt_horizontal_ingress": rrt_horizontal_ingress,
        "container_state_source": state_source,
        "container_support_geom": support_geom,
        "validated_open_gripper": bool(validate_open_gripper),
        "release_fingers": release_fingers,
    })
    if return_planning_details:
        return track, waypoints[-1].copy(), {
            "forward": [waypoint.copy() for waypoint in forward],
            "scene_q": scene_q.copy(),
        }
    return track, waypoints[-1].copy()


def gen_pick_reachin(
        rig, q_start, obj_name, interior_body, label=None,
        state_source="current", support_geom=None, front_distance=None,
        ik_eef_tolerance=0.012, randomize_preinsert_first=False,
        ik_top_down=False, rrt_horizontal_ingress=False,
        reachin_lift_height=None, cartesian_rrt_fallback=False,
        nearby_rrt_fallback=False, grasp_offset=None,
        calibrated_track_path=None):
    """Load a frame-validated deterministic front-container pick corridor.

    Runtime seed search is intentionally disabled.  Study-scene reach-ins are
    calibrated once, then replayed as outside/open -> reach in -> close/attach
    -> withdraw.  A missing calibration fails immediately instead of entering
    the former multi-minute IK/RRT retry loop.
    """
    label = label or f"pick_{obj_name}_reachin"
    scene_q, container_root = _container_scene_state(
        rig, q_start, interior_body, state_source=state_source)
    rig.set_fingers(scene_q, rig.finger_open)

    calibrated_track_path = (
        None if calibrated_track_path is None
        else Path(calibrated_track_path))
    if calibrated_track_path is not None and calibrated_track_path.exists():
        track = json.loads(calibrated_track_path.read_text("utf-8"))
        track["meta"]["skill"] = label
        track["meta"]["planner"] = "calibrated_single_seed_reachin_pick"
        track["meta"]["grasp_mode"] = "reachin"
        track["meta"]["reachin_seed_attempts"] = 0
        track["meta"]["reachin_calibrated_track"] = str(
            calibrated_track_path)
        q_end = _apply_track_last_frame(rig, scene_q.copy(), track)
        off = _grasp_offset(rig, obj_name, q_end)
        return track, q_end, off
    raise ValueError(
        f"reach-in pick for '{obj_name}' has no calibrated track; "
        "online IK/RRT seed search is disabled")

    obj_adr = rig.jadr(_obj_joint(obj_name))
    object_pose = scene_q[obj_adr:obj_adr + 7].copy()
    object_pos = object_pose[:3]
    base_xy = rig.base_xy(scene_q)
    approach = np.array([
        float(object_pos[0] - base_xy[0]),
        float(object_pos[1] - base_xy[1]),
        0.0,
    ])
    approach_norm = float(np.linalg.norm(approach))
    if approach_norm < 1e-6:
        raise ValueError(
            f"cannot infer reverse reach-in direction for '{obj_name}'")
    approach /= approach_norm

    m, d = rig.model, rig.data
    arm_root = mujoco.mj_name2id(
        m, mujoco.mjtObj.mjOBJ_BODY, f"robot{rig.robot}_link0")
    arm_geoms = _body_subtree_geoms(m, arm_root)
    container_geoms = _body_subtree_geoms(m, container_root)

    def endpoint_valid(candidate):
        d.qpos[:] = candidate
        mujoco.mj_forward(m, d)
        for contact_index in range(d.ncon):
            contact = d.contact[contact_index]
            g1, g2 = int(contact.geom1), int(contact.geom2)
            if ((g1 in arm_geoms and g2 in container_geoms)
                    or (g2 in arm_geoms and g1 in container_geoms)):
                return False
        return True

    grasp_offset = np.zeros(3) if grasp_offset is None else np.asarray(
        grasp_offset, dtype=float)
    if grasp_offset.shape != (3,) or not np.all(np.isfinite(grasp_offset)):
        raise ValueError("reachin grasp_offset must be a finite [x, y, z]")
    grasp_target = object_pos + grasp_offset
    reachin_front_distance = (
        REACHIN_FRONT_DISTANCE if front_distance is None
        else float(front_distance))
    reachin_lift_height = (
        REACHIN_LIFT_HEIGHT if reachin_lift_height is None
        else float(reachin_lift_height))
    open_fingers = [0.02, -0.02]
    entry_seed = REACHIN_ENTRY_SEED_BY_OBJECT.get(obj_name)
    if entry_seed is None:
        raise ValueError(
            f"reach-in pick for '{obj_name}' has no calibrated single seed")

    # One deterministic continuation seed only: each waypoint starts from the
    # preceding solution.  A failure is reported immediately; reach-in pick
    # deliberately does not randomize or try a bank of alternate IK seeds.
    ingress = [scene_q.copy()]
    rig.set_fingers(ingress[0], open_fingers)
    eef_start, _ = rig.eef_pose(ingress[0])
    outside_high = (
        grasp_target - reachin_front_distance * approach
        + np.array([0.0, 0.0, reachin_lift_height]))
    inside_high = grasp_target + np.array(
        [0.0, 0.0, reachin_lift_height])
    stage_targets = [
        ("to-entry", outside_high),
        ("horizontal-insert", inside_high),
        ("vertical-lower", grasp_target),
    ]
    previous_eef = eef_start.copy()
    waypoint_diagnostics = []
    for stage_name, stage_target in stage_targets:
        distance = float(np.linalg.norm(stage_target - previous_eef))
        count = (
            1 if stage_name == "to-entry"
            else max(1, int(np.ceil(distance / REACHIN_CARTESIAN_STEP))))
        stage_start = previous_eef.copy()
        for waypoint_index, alpha in enumerate(
                np.linspace(0.0, 1.0, count + 1)[1:], start=1):
            eef_target = (
                (1.0 - alpha) * stage_start + alpha * stage_target)
            # Keep the tool facing the object along the robot-to-object access
            # ray. Do not literally "look at" the centre from every nearby
            # waypoint: at the offset edge grasp that vector turns 90 degrees
            # sideways and drives link4 into the refrigerator door.
            look_at = approach
            candidate = ingress[-1].copy()
            if stage_name == "to-entry":
                candidate[rig.TORSO] = float(entry_seed["torso"])
                candidate[rig.ARM] = entry_seed["arm"]
            rig.set_fingers(candidate, open_fingers)
            for _ in range(3):
                rig.ik_arm(
                    candidate, eef_target, top_down=False,
                    approach_world=look_at, up_world=WORLD_UP)
            actual_eef, _ = rig.eef_pose(candidate)
            rotation = d.xmat[rig.eef].reshape(3, 3).copy()
            position_error = float(np.linalg.norm(actual_eef - eef_target))
            approach_error = float(np.linalg.norm(np.cross(
                rotation @ APPROACH_LOCAL, look_at)))
            up_error = float(np.linalg.norm(np.cross(
                rotation @ GRIPPER_UP_LOCAL, WORLD_UP)))
            if (position_error > ik_eef_tolerance
                    or approach_error > 0.35 or up_error > 0.25):
                raise ValueError(
                    f"single-seed reach-in {stage_name} waypoint "
                    f"{waypoint_index}/{count} failed "
                    f"(position={position_error:.4f}, "
                    f"approach={approach_error:.4f}, up={up_error:.4f})")
            if not endpoint_valid(candidate):
                collision_names = []
                for contact_index in range(d.ncon):
                    contact = d.contact[contact_index]
                    g1, g2 = int(contact.geom1), int(contact.geom2)
                    if ((g1 in arm_geoms and g2 in container_geoms)
                            or (g2 in arm_geoms and g1 in container_geoms)):
                        collision_names.append((
                            mujoco.mj_id2name(
                                m, mujoco.mjtObj.mjOBJ_GEOM, g1),
                            mujoco.mj_id2name(
                                m, mujoco.mjtObj.mjOBJ_GEOM, g2),
                        ))
                raise ValueError(
                    f"single-seed reach-in {stage_name} waypoint "
                    f"{waypoint_index}/{count} collides with container: "
                    f"{collision_names}")
            ingress.append(candidate)
            waypoint_diagnostics.append({
                "stage": stage_name,
                "position_error": position_error,
                "approach_error": approach_error,
                "up_error": up_error,
            })
        previous_eef = stage_target

    grasp_q = ingress[-1]
    eef_pos, eef_quat = rig.eef_pose(grasp_q)
    off_pos = quat_rot(quat_conj(eef_quat), object_pos - eef_pos)
    off_quat = quat_mul(quat_conj(eef_quat), object_pose[3:7])
    off = (off_pos, off_quat)
    closed = ingress[-1].copy()
    rig.set_fingers(closed, rig.finger_closed)
    egress = []
    for waypoint in reversed(ingress[:-1]):
        waypoint = waypoint.copy()
        rig.set_fingers(waypoint, rig.finger_closed)
        egress.append(waypoint)
    waypoints = ingress + [closed] + egress
    pick_plan_adrs = [rig.TORSO, *rig.ARM]
    durations = [
        max(
            0.15,
            float(np.max(np.abs(b[pick_plan_adrs] - a[pick_plan_adrs])))
            / RRT_ARM_SPEED,
        )
        for a, b in zip(waypoints, waypoints[1:])
    ]
    durations[len(ingress) - 1] = max(
        0.4, durations[len(ingress) - 1])
    obj = {
        "name": obj_name,
        "attach_from_seg": len(ingress),
        "off_pos": off_pos,
        "off_quat": off_quat,
        "static_pose": (object_pose[:3], object_pose[3:7]),
    }
    track = build_track(rig, label, waypoints, durations, obj)

    # Validate the motion that will actually be replayed.  The forward place
    # template checks a closed gripper carrying the object on ingress; after
    # reversal, ingress uses an open (wider) empty gripper, so validating only
    # the template could miss a finger/door collision.
    object_root = mujoco.mj_name2id(
        m, mujoco.mjtObj.mjOBJ_BODY, f"{obj_name}_main")
    object_geoms = _body_subtree_geoms(m, object_root)
    channel_adrs = {
        name: (rig.jadr(name), len(values[0]))
        for name, values in track["channels"].items()
    }
    baseline_object_pairs = set()
    replay_q = scene_q.copy()
    for name, values in track["channels"].items():
        address, width = channel_adrs[name]
        replay_q[address:address + width] = values[0]
    d.qpos[:] = replay_q
    mujoco.mj_forward(m, d)
    for contact_index in range(d.ncon):
        contact = d.contact[contact_index]
        g1, g2 = int(contact.geom1), int(contact.geom2)
        if ((g1 in object_geoms and g2 in container_geoms)
                or (g2 in object_geoms and g1 in container_geoms)):
            baseline_object_pairs.add((min(g1, g2), max(g1, g2)))

    for frame_index in range(len(track["time"])):
        replay_q[:] = scene_q
        for name, values in track["channels"].items():
            address, width = channel_adrs[name]
            replay_q[address:address + width] = values[frame_index]
        d.qpos[:] = replay_q
        mujoco.mj_forward(m, d)
        for contact_index in range(d.ncon):
            contact = d.contact[contact_index]
            g1, g2 = int(contact.geom1), int(contact.geom2)
            arm_container = (
                (g1 in arm_geoms and g2 in container_geoms)
                or (g2 in arm_geoms and g1 in container_geoms))
            object_container = (
                (g1 in object_geoms and g2 in container_geoms)
                or (g2 in object_geoms and g1 in container_geoms))
            pair = (min(g1, g2), max(g1, g2))
            if arm_container or (
                    object_container and pair not in baseline_object_pairs):
                raise ValueError(
                    "reach-in pick replay collides with container at "
                    f"frame {frame_index}/{len(track['time']) - 1}: "
                    f"{mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, g1)} <-> "
                    f"{mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, g2)}")

    track["meta"].update({
        "planner": "single_seed_cartesian_reachin_pick",
        "grasp_mode": "reachin",
        "reachin_corridor_source": "direct_cartesian",
        "reachin_seed_attempts": 1,
        "reachin_waypoints": len(ingress),
        "reachin_waypoint_diagnostics": waypoint_diagnostics,
        "reachin_replay_collision_checks": len(track["time"]),
        "reachin_grasp_offset": [float(value) for value in grasp_offset],
        "rrt_path_collision_free": False,
    })
    q_end = _apply_track_last_frame(rig, scene_q.copy(), track)
    return track, q_end, off


def gen_wait(rig, q_start, dur, carrying=None):
    obj = {**carrying, "attach_from_seg": 0} if carrying else None
    return build_track(rig, "wait", [q_start, q_start], [dur], obj)


def gen_reset(rig, q_start, retreat=RESET_RETREAT_DEFAULT,
              preserve_yaw=True, label="reset_ready"):
    """Back away with fixed yaw, then return the empty arm to ready.

    The base retreat and arm reset are separate collision-checked phases. A
    direct joint interpolation is preferred; if it is blocked, a deterministic
    local RRT-Connect plans torso+arm back to the ready configuration.
    """
    retreat = float(retreat)
    if retreat < 0.0:
        raise ValueError("reset retreat must be non-negative")
    if preserve_yaw is not True:
        raise ValueError("reset currently requires preserve_yaw=true")

    start = q_start.copy()
    valid = _robot_collision_checker(rig, start)
    start_xy = np.asarray(rig.base_xy(start), dtype=float)
    forward = rig.base_forward(start)
    base_adrs = [rig.FWD, rig.SIDE, rig.YAW]
    base_edge_res = [RESET_BASE_EDGE_RES, RESET_BASE_EDGE_RES, 0.05]

    # Retreat as far as comfortably possible (up to `retreat`): scan from the
    # requested distance down to 0 and keep the largest backward standoff whose
    # straight base slide stays collision-free. A larger standoff pulls the arm
    # clear of an open container so the arm reset below is a trivial straight
    # line rather than an expensive local RRT. Distance 0 (no base move) is
    # always feasible, so this never hard-fails the way a single fixed retreat
    # did when that exact distance happened to collide.
    n_steps = max(2, int(round(retreat / RESET_RETREAT_STEP)) + 1)
    chosen_retreat = 0.0
    backed = start.copy()
    for d in np.linspace(retreat, 0.0, n_steps):
        candidate = start.copy()
        rig.place_base_preserve_yaw(candidate, start_xy - float(d) * forward)
        if d <= 1e-6 or _qpos_edge_collision_free(
                start, candidate, base_adrs, base_edge_res, valid):
            chosen_retreat = float(d)
            backed = candidate
            break

    ready = backed.copy()
    rig.apply_ready(ready)
    rig.set_fingers(ready, rig.finger_open)
    arm_adrs = [rig.TORSO, *rig.ARM, *rig.FINGERS]
    arm_res = [
        RRT_TORSO_EDGE_RES,
        *([RESET_ARM_EDGE_RES] * len(rig.ARM)),
        0.01,
        0.01,
    ]
    arm_mode = "linear"
    rrt_iterations = 0
    if _qpos_edge_collision_free(backed, ready, arm_adrs, arm_res, valid):
        arm_path = [backed, ready]
    else:
        arm_mode = "local_rrt"
        plan_adrs = [rig.TORSO, *rig.ARM]
        torso_jid = mujoco.mj_name2id(
            rig.model, mujoco.mjtObj.mjOBJ_JOINT,
            f"mobilebase{rig.robot}_joint_torso_height")
        limits = [tuple(map(float, rig.model.jnt_range[torso_jid]))]
        for k, value in enumerate(backed[rig.ARM]):
            if rig.ARM_LIMITED[k]:
                limits.append(tuple(map(float, rig.ARM_RANGE[k])))
            else:
                limits.append((float(value - np.pi), float(value + np.pi)))

        def q_for_config(config):
            q = backed.copy()
            q[plan_adrs] = config
            rig.set_fingers(q, rig.finger_open)
            return q

        def config_valid(config):
            return valid(q_for_config(config))

        rng = np.random.default_rng(RRT_SEED + 2000 + rig.robot)
        raw, rrt_iterations, edge_valid = _rrt_connect(
            backed[plan_adrs], ready[plan_adrs], config_valid, limits, rng,
            edge_res=[
                RRT_TORSO_EDGE_RES,
                *([RESET_ARM_EDGE_RES] * len(rig.ARM)),
            ],
            step_scale=[
                RRT_TORSO_STEP,
                *([RRT_STEP] * len(rig.ARM)),
            ],
            sample_res=[
                RRT_TORSO_SAMPLE_RES,
                *([None] * len(rig.ARM)),
            ],
        )
        smooth = _shortcut_path(raw, edge_valid, rng)
        arm_path = [q_for_config(config) for config in smooth]

    waypoints = [start, *arm_path]
    retreat_duration = max(0.2, chosen_retreat / BASE_SPEED)
    durations = [retreat_duration]
    for a, b in zip(arm_path, arm_path[1:]):
        torso_time = abs(float(b[rig.TORSO] - a[rig.TORSO])) / 0.08
        arm_time = float(np.max(np.abs(b[rig.ARM] - a[rig.ARM]))) / RRT_ARM_SPEED
        finger_time = float(np.max(np.abs(b[rig.FINGERS] - a[rig.FINGERS]))) / 0.04
        durations.append(max(0.2, torso_time, arm_time, finger_time))

    track = build_track(rig, label, waypoints, durations)
    end_xy = np.asarray(rig.base_xy(waypoints[-1]), dtype=float)
    track["meta"].update({
        "retreat_distance": float(np.linalg.norm(end_xy - start_xy)),
        "preserve_yaw": True,
        "yaw_change": float(_wrap_pi(
            waypoints[-1][rig.YAW] - start[rig.YAW])),
        "reset_collision_free": True,
        "arm_reset_mode": arm_mode,
        "reset_rrt_iterations": rrt_iterations,
    })
    return track, waypoints[-1].copy()


# ---------------------------------------------------------------- plan compile
def _carry(rig, obj_name, off):
    return {
        "name": obj_name, "off_pos": off[0], "off_quat": off[1],
        "static_pose": _rest_obj_pose(rig, obj_name),
    }


# Placement regions: a small area inside each facility where objects are laid
# out. Two kinds (see docs/container_placement_design.md):
#   - surface (default, e.g. sink/counter): center = facility body world pos +
#     z_offset; `half` = XY half-extents.
#   - container (e.g. an open drawer): no precomputed region — `interior_body`
#     names the moving part whose CURRENT world position (post-open) dest_point
#     reads at compile time; see _container_dest_point.
# `access` selects the placement motion generator in compile_robot: "top"
# uses gen_place for both kinds; "front" (horizontal reach-in, RRT) serves
# cabinet/fridge.
#
# Scene-specific placement configuration — interior bodies, support geoms,
# per-fixture motion tuning — lives in the generated skills manifest
# (facilities.<name>.place, built by build_skills_manifest from the scene
# table). This module holds NO concrete fixture names.
PLACEMENT_REGIONS: dict = {}


def placement_regions_from_manifest(facilities: dict) -> dict:
    """Build compile-local placement regions from a scene manifest.

    Surface facilities contribute a static region (center/half computed at
    manifest build time) plus optional fixed world-frame ``slot_points`` and
    object-specific ``object_slot_points``.
    Container facilities contribute their complete
    placement configuration verbatim — access mode, interior body, support
    geom, state source, and motion tuning — exactly as generated into
    ``facilities.<name>.place`` by build_skills_manifest. Never consult a
    global table here: a service can compile more than one scene in one
    process and a previous scene's fixtures must never leak into a later one.
    """
    regions = {}
    for name, facility in facilities.items():
        place = (facility or {}).get("place") or {}
        if place.get("kind") == "container":
            if not place.get("interior_body"):
                raise ValueError(
                    f"container facility '{name}' has no interior_body in the "
                    "manifest; regenerate it with build_skills_manifest (schema v2)")
            region = {k: v for k, v in place.items() if k != "provenance"}
            regions[name] = region
            continue
        raw = place.get("region")
        if raw is None:
            continue
        try:
            center_xy = raw["center_xy"]
            half = raw["half"]
            center = [float(center_xy[0]), float(center_xy[1]), float(raw["z"])]
            normalized_half = [float(half[0]), float(half[1])]
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ValueError(
                f"invalid surface placement region for facility '{name}'") from exc
        if any(value <= 0 for value in normalized_half):
            raise ValueError(f"invalid surface placement half extents for '{name}'")
        regions[name] = {"kind": "surface", "center": center,
                         "half": normalized_half}
        if place.get("slot_points") is not None:
            regions[name]["slot_points"] = place["slot_points"]
        if place.get("object_slot_points") is not None:
            regions[name]["object_slot_points"] = place["object_slot_points"]
    return regions


def _fixed_slot_point(facility, region, slot, n):
    """Return a configured world-frame slot, or ``None`` for auto layout."""
    points = region.get("slot_points")
    if points is None:
        return None
    if n > len(points):
        raise ValueError(
            f"placement capacity exceeded for '{facility}': requested {n} "
            f"objects but only {len(points)} fixed slot(s) are configured")
    if slot < 0 or slot >= n:
        raise ValueError(
            f"invalid placement slot {slot} of {n} for '{facility}'")
    point = np.asarray(points[slot], dtype=float)
    if point.ndim != 1 or len(point) not in (2, 3) or not np.all(np.isfinite(point)):
        raise ValueError(
            f"invalid fixed placement point {slot} for '{facility}': "
            "expected finite [x, y] or [x, y, z]")
    return point


def _slot_fractions(n, spread=0.70, slot_fracs=None):
    if slot_fracs is not None:
        # A container region may define preferred lanes for a multi-object
        # plan while also being used by a single-object plan.  Keep the first
        # lane in that case (e.g. apple alone uses its proven lane); only use
        # the custom list when it supplies exactly the requested number.
        if n == 1:
            fracs = [float(slot_fracs[0])]
        elif len(slot_fracs) == n:
            fracs = [float(value) for value in slot_fracs]
        else:
            fracs = np.linspace(-float(spread), float(spread), n)
    else:
        fracs = [0.0] if n <= 1 else np.linspace(-float(spread), float(spread), n)
    return fracs


def distribute_slots(center, half, n, spread=0.70, slot_fracs=None):
    """n evenly-spaced points inside an axis-aligned region."""
    center = np.asarray(center, dtype=float)
    axis = 0 if half[0] >= half[1] else 1
    span = half[axis]
    fracs = _slot_fractions(n, spread=spread, slot_fracs=slot_fracs)
    pts = []
    for fr in fracs:
        p = center.copy()
        p[axis] += fr * span
        pts.append(p)
    return pts


REACH_BAND = (0.40, 0.90)   # 2D base->target distance that counts as reachable
BASE_CLEAR = 0.35           # base center kept this far from furniture edges
                            # (matches the validated island GAP), metres
ROOM_INSET = 0.10           # keep the standoff this far inside the room bounds
FLOOR_AREA = 6.0            # geoms with a larger XY footprint (floor/ceiling)
                            # are not obstacles, m^2


def _geom_xy_aabb(model, data, g):
    """Rotation-aware XY footprint of a collision geom (None to skip)."""
    gtype = model.geom_type[g]
    if gtype == mujoco.mjtGeom.mjGEOM_PLANE:
        return None
    p = data.geom_xpos[g]
    if p[2] > 2.2 or p[2] < -0.1:
        return None
    if gtype == mujoco.mjtGeom.mjGEOM_BOX:
        s = model.geom_size[g]
        R = data.geom_xmat[g].reshape(3, 3)
        c = np.array([p + R @ (np.array([sx, sy, sz]) * s)
                      for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])
        return c[:, 0].min(), c[:, 0].max(), c[:, 1].min(), c[:, 1].max()
    r = float(model.geom_rbound[g])
    if r <= 0 or r > 2.5:
        return None
    return p[0] - r, p[0] + r, p[1] - r, p[1] + r


def furniture_footprints(rig, working_q=None, exclude_bodies=()):
    """XY AABBs of all collision geoms (excluding robots + given bodies),
    computed from `working_q` so open drawers/doors are included."""
    m, d = rig.model, rig.data
    if working_q is not None:
        d.qpos[:] = working_q
    mujoco.mj_forward(m, d)
    rects = []
    for g in range(m.ngeom):
        if m.geom_group[g] != 0:
            continue
        bn = mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_BODY, m.geom_bodyid[g]) or ""
        if bn.startswith(("robot", "mobilebase", "gripper", "world")) or bn in exclude_bodies:
            continue
        ab = _geom_xy_aabb(m, d, g)
        if ab and (ab[1] - ab[0]) * (ab[3] - ab[2]) <= FLOOR_AREA:  # skip floor/ceiling slabs
            rects.append(ab)
    return rects


def _clearance(p, rects):
    """Distance from point p to the nearest furniture AABB (0 if inside one)."""
    best = 1e9
    for x0, x1, y0, y1 in rects:
        dx = max(x0 - p[0], 0.0, p[0] - x1)
        dy = max(y0 - p[1], 0.0, p[1] - y1)
        best = min(best, (dx * dx + dy * dy) ** 0.5)
    return best


def _floor_xy_bounds(rig):
    """Largest finite floor footprint, used as the actual room boundary.

    Open study scenes deliberately remove one or more wall bodies.  Inferring
    room bounds from the remaining furniture then clips valid standoffs on the
    open side of an island (layout034's plate_2 side is one such case).  The
    exported finite floor slab still describes the navigable room exactly.
    """
    m, d = rig.model, rig.data
    candidates = []
    for g in range(m.ngeom):
        name = mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, g) or ""
        if "floor" not in name.lower():
            continue
        bounds = _geom_xy_aabb(m, d, g)
        if bounds is None:
            continue
        area = (bounds[1] - bounds[0]) * (bounds[3] - bounds[2])
        if area > FLOOR_AREA:
            candidates.append((area, bounds))
    return max(candidates, default=(None, None), key=lambda item: item[0])[1]


def standoff_for_point(rig, target_xy, working_q=None, exclude_bodies=(),
                       reach=REACH_BAND, base_clear=BASE_CLEAR):
    """Find a floor base position facing `target_xy`, clear of furniture, within
    2D arm reach. Reachability is a simple planar distance test (no 3D IK), per
    design. Returns {feasible, standoff_xy, face_xy, reason, clearance}.

    Exposed as an LLM capability: the LLM proposes a point (e.g. inside a rough
    user-given region) and calls this to check/realize a base pose."""
    target_xy = np.asarray(target_xy, dtype=float)[:2]
    rects = furniture_footprints(rig, working_q, exclude_bodies)
    # Prefer the finite floor slab over furniture/walls.  Study exports can
    # intentionally remove enclosing walls, while the floor remains the stable
    # source of truth for valid room extents.
    floor_bounds = _floor_xy_bounds(rig)
    if floor_bounds is None:
        floor_bounds = (
            min(r[0] for r in rects), max(r[1] for r in rects),
            min(r[2] for r in rects), max(r[3] for r in rects),
        )
    rminx = floor_bounds[0] + ROOM_INSET
    rmaxx = floor_bounds[1] - ROOM_INSET
    rminy = floor_bounds[2] + ROOM_INSET
    rmaxy = floor_bounds[3] - ROOM_INSET

    # among candidates clear of furniture and in the room, prefer the shortest
    # reach distance (base tucked close to the counter, like the island GAP),
    # tie-broken by more clearance.
    best = None
    for ang in np.linspace(0, 2 * np.pi, 24, endpoint=False):
        d_hat = np.array([np.cos(ang), np.sin(ang)])
        for dist in (0.45, 0.55, 0.65, 0.75, 0.85):
            if not (reach[0] <= dist <= reach[1]):
                continue
            cand = target_xy + d_hat * dist
            if not (rminx <= cand[0] <= rmaxx and rminy <= cand[1] <= rmaxy):
                continue
            clr = _clearance(cand, rects)
            if clr <= base_clear:
                continue
            score = (-round(dist, 3), clr)  # smaller dist first, then more clearance
            if best is None or score > best[0]:
                best = (score, cand, clr)
    if best is None:
        return {"feasible": False, "reason": "no clear floor position within reach",
                "standoff_xy": None, "face_xy": [round(float(v), 3) for v in target_xy]}
    _, cand, clr = best
    return {"feasible": True, "reason": "ok", "clearance": round(float(clr), 3),
            "standoff_xy": [round(float(cand[0]), 3), round(float(cand[1]), 3)],
            "face_xy": [round(float(target_xy[0]), 3), round(float(target_xy[1]), 3)]}


def _pinned_surface_standoff(rig, facility, next_step, q,
                             placement_regions, navigate_step=None):
    """Compute a request-local stance for a navigate feeding a pinned place.

    Static ``standoffs.json`` entries remain the facility default. A surface
    runtime pin, object-specific manifest slot, or materially edited placement
    point is more specific: stand within arm reach of that point and face it, so
    the arm does not compensate sideways from the counter midpoint. Container
    anchors are excluded until their moving local frame is supported.
    """
    if not next_step or next_step.get("op") != "place":
        return None
    if next_step.get("dest") != facility:
        return None
    region = placement_regions.get(facility) or {}
    if region.get("kind", "surface") != "surface":
        return None
    # ``at`` is the scene-dragged, final placement point. Completed plans also
    # contain a compiler-echoed ``at``, so do not blindly prefer it: compare it
    # with the placement reference that produced the old stance and only
    # invalidate that stance once the point moved far enough to matter. With
    # no previous metadata (a freshly authored explicit point), solve from
    # ``at``.
    anchor = None
    at = next_step.get("at")
    previous_face = (
        navigate_step.get("face_xy")
        if isinstance(navigate_step, dict) else None)
    # New completed plans carry the exact placement point from which their
    # current stance was solved.  This avoids mistaking an automatically
    # distributed point (which may be far from a facility's generic face_xy)
    # for a user edit on an otherwise untouched round trip. ``previous_face``
    # remains the compatibility reference for completed plans made before this
    # field existed.
    reference = next_step.get("standoff_at")
    if not (isinstance(reference, (list, tuple)) and len(reference) >= 2):
        reference = previous_face
    if isinstance(at, (list, tuple)) and len(at) >= 2:
        if not (isinstance(reference, (list, tuple))
                and len(reference) >= 2):
            anchor = at
        else:
            moved = float(np.linalg.norm(
                np.asarray(at[:2], dtype=float)
                - np.asarray(reference[:2], dtype=float)))
            if moved > PLACEMENT_STANDOFF_RECOMPUTE_DISTANCE:
                anchor = at
    if anchor is None:
        anchor = next_step.get("at_anchor")
    if not isinstance(anchor, (list, tuple)) or len(anchor) < 2:
        object_name = next_step.get("object")
        anchor = (region.get("object_slot_points") or {}).get(object_name)
    if not isinstance(anchor, (list, tuple)) or len(anchor) < 2:
        return None
    result = standoff_for_point(rig, anchor[:2], working_q=q)
    if result.get("feasible"):
        return result
    # An explicit user pin is an exact spatial request. Falling back to the
    # facility's generic midpoint stance when that pin has no reachable stance
    # lets place IK silently saturate and produces an object suspended near the
    # gripper instead of at the pin. Reject the request clearly; automatic
    # slots retain their historical generic-standoff fallback.
    if next_step.get("at_anchor") is not None:
        xy = [round(float(value), 3) for value in anchor[:2]]
        raise ValueError(
            f"pinned place on '{facility}' at {xy} has no reachable robot "
            f"standoff: {result.get('reason', 'unknown reason')}")
    return None


def _interior_floor_geom(model, interior_body, support_geom=None):
    """The interior_body's own bottom collision geom (group 0, name containing
    "bottom") — e.g. a drawer's inner floor slab. Not hand-typed: found by
    walking the body's own geoms, so it stays correct if tracks/geometry are
    regenerated."""
    bid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, interior_body)
    if bid < 0:
        raise ValueError(f"unknown interior_body '{interior_body}'")
    own_geoms = range(model.body_geomadr[bid], model.body_geomadr[bid] + model.body_geomnum[bid])
    if support_geom is not None:
        g = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, support_geom)
        if g < 0:
            raise ValueError(f"unknown container support geom '{support_geom}'")
        # Some RoboCasa fixtures keep the semantic interior marker in a child
        # body while the physical shelf slabs live on its parent fixture body
        # (Refrigerator040 is one example).  An explicit support may therefore
        # be owned by the interior body or any of its ancestors, but never by
        # an unrelated sibling / fixture.
        support_body = int(model.geom_bodyid[g])
        owner = bid
        while owner > 0 and owner != support_body:
            owner = int(model.body_parentid[owner])
        if owner != support_body:
            raise ValueError(
                f"support geom '{support_geom}' is not owned by "
                f"interior_body '{interior_body}' or one of its ancestors")
        if model.geom_group[g] != 0:
            raise ValueError(f"support geom '{support_geom}' is not a collision geom")
        return g
    for g in own_geoms:
        if model.geom_group[g] != 0:
            continue
        gname = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, g) or ""
        if "bottom" in gname:
            return g
    # Phase B cabinet levels are themselves thin shelf bodies, named "shelf"
    # rather than "bottom". This fallback is after the drawer-specific match,
    # so Phase A continues to select the exact same drawer floor geom.
    for g in own_geoms:
        if model.geom_group[g] != 0:
            continue
        gname = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, g) or ""
        if "shelf" in gname:
            return g
    raise ValueError(
        f"no interior support geom (group0, name contains 'bottom' or 'shelf') "
        f"under '{interior_body}'")


def _container_front_direction(rig, interior_body, q, support_geom=None):
    """Shelf-local unit XY axis facing the robot/container opening."""
    m, d = rig.model, rig.data
    d.qpos[:] = q
    mujoco.mj_forward(m, d)
    g = _interior_floor_geom(m, interior_body, support_geom=support_geom)
    center = d.geom_xpos[g][:2]
    rotation = d.geom_xmat[g].reshape(3, 3)
    toward_base = rig.base_xy(q) - center
    toward_base /= max(float(np.linalg.norm(toward_base)), 1e-9)
    lateral = np.array([-toward_base[1], toward_base[0]])
    lateral_axis = max(
        (0, 1),
        key=lambda candidate: abs(float(
            np.dot(rotation[:2, candidate], lateral))),
    )
    front = rotation[:2, 1 - lateral_axis].copy()
    if float(np.dot(front, toward_base)) < 0.0:
        front *= -1.0
    return front / max(float(np.linalg.norm(front)), 1e-9)


def _container_dest_point(rig, interior_body, q, slot, n, at,
                          support_geom=None, front_access=False,
                          front_target_offset=0.10, slot_spread=0.70,
                          slot_fracs=None, front_target_max_fraction=0.70,
                          slot_center_frac=0.0, anchor=None):
    """Placement point inside a container (e.g. an open drawer).

    A container's interior has physically moved from its rest pose once open
    (a drawer slides ~0.6m along its own axis) — its world position must be
    read from the CURRENTLY COMPILED pose `q`, explicitly synced into
    rig.data here, never qpos0. (Rig.surface_z forces qpos0 for the surface
    case above, which is correct for a static counter but would silently
    ray-cast the CLOSED drawer's geometry here.) Reuses the same
    ray-cast-for-z + distribute_slots technique as the surface path, just
    against the current state and the interior floor geom's own footprint
    instead of a hand-typed region.
    """
    m, d = rig.model, rig.data
    d.qpos[:] = q
    mujoco.mj_forward(m, d)

    g = _interior_floor_geom(m, interior_body, support_geom=support_geom)
    gxy = d.geom_xpos[g][:2].copy()
    half = [float(m.geom_size[g][0]), float(m.geom_size[g][1])]
    gname = mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, g) or ""
    R = d.geom_xmat[g].reshape(3, 3)

    front_dir = None
    if front_access:
        # Choose the shelf-local axis that is lateral to the opening. This is
        # used by both default slots and multiple objects sharing one pin.
        toward_base = rig.base_xy(q) - gxy
        toward_base /= max(float(np.linalg.norm(toward_base)), 1e-9)
        lateral = np.array([-toward_base[1], toward_base[0]])
        axis = max(
            (0, 1),
            key=lambda candidate: (
                abs(float(np.dot(R[:2, candidate], lateral))),
                half[candidate],
            ),
        )
        front_dir = _container_front_direction(
            rig, interior_body, q, support_geom=support_geom)
    else:
        axis = 0 if half[0] >= half[1] else 1

    if at is not None:
        p = np.asarray(at, dtype=float)
        x, y = float(p[0]), float(p[1])
        z = float(p[2]) if len(p) >= 3 else None
    elif anchor is not None:
        p = np.asarray(anchor, dtype=float)
        if p.ndim != 1 or len(p) < 2 or not np.isfinite(p[:2]).all():
            raise ValueError("container placement anchor must be finite [x, y]")

        # A scene pin is authored in world XY, while a drawer/cabinet support
        # can be translated and rotated in its current open state. Project the
        # pin into the live support geom's local XY frame, distribute multiple
        # objects laterally around it, then clamp the result inside the shelf.
        local_xy = np.linalg.lstsq(
            R[:2, :2], p[:2] - gxy, rcond=None)[0]
        fracs = _slot_fractions(
            n, spread=slot_spread, slot_fracs=slot_fracs)
        local_xy[axis] += float(fracs[slot]) * half[axis]
        # Keep the object center just inside the support boundary. Collision
        # validation still owns exact object-shape feasibility downstream.
        usable_half = np.maximum(np.asarray(half) - 0.02, 0.0)
        local_xy = np.clip(local_xy, -usable_half, usable_half)
        world_xy = gxy + R[:2, :2] @ local_xy
        x, y, z = float(world_xy[0]), float(world_xy[1]), None
    else:
        fracs = _slot_fractions(
            n, spread=slot_spread, slot_fracs=slot_fracs)
        slot_pt = np.array([gxy[0], gxy[1], 0.0])
        lateral_frac = float(fracs[slot]) + float(slot_center_frac)
        if abs(lateral_frac) >= 1.0:
            raise ValueError("container lateral slot fraction must stay inside (-1, 1)")
        slot_pt[:2] += (
            lateral_frac * half[axis] * R[:2, axis])
        if front_access or "shelf" in gname:
            # Aim inside the open edge rather than at the deep geometric center.
            # Cap by the oriented shelf extent so the point stays on the board.
            shift_dir = front_dir
            if shift_dir is None:
                shift_dir = rig.base_xy(q) - gxy
                shift_dir /= max(float(np.linalg.norm(shift_dir)), 1e-9)
            extent = float(sum(
                abs(float(np.dot(shift_dir, R[:2, candidate])))
                * m.geom_size[g][candidate]
                for candidate in (0, 1)
            ))
            max_fraction = float(front_target_max_fraction)
            if not 0.0 < max_fraction < 1.0:
                raise ValueError(
                    "front_target_max_fraction must be between 0 and 1")
            slot_pt[:2] += shift_dir * min(
                float(front_target_offset), max_fraction * extent)
        x, y, z = float(slot_pt[0]), float(slot_pt[1]), None

    if z is None:
        # The support geom is already known. Its oriented top face is stable
        # even while another object or the robot arm sits above it; ray-casting
        # the second placement used to hit that dynamic geometry and raise a
        # drawer target from ~0.66 m to ~0.92 m, leaving the mug suspended.
        R = d.geom_xmat[g].reshape(3, 3)
        z = float(d.geom_xpos[g][2] + np.sum(np.abs(R[2]) * m.geom_size[g]))
    return np.array([x, y, z])


def dest_point(rig, facility, q, slot=0, n=1, at=None, anchor=None,
               placement_regions=None, object_name=None):
    """World placement point for the `slot`-th of `n` objects placed at
    `facility`. Two kinds (PLACEMENT_REGIONS[facility]["kind"], default
    "surface"):
    - surface: drop point on an open surface (sink/counter). The z is the real
      surface height under (x, y), raycast down onto the facility (against
      qpos0 — a static body never moves, so an in-flight arm can't shadow it).
    - container: drop point inside an open container — see
      _container_dest_point (needs the CURRENT `q`, not qpos0, since the
      container has physically moved).
    An explicit `at` overrides xy (and z too, when given as [x, y, z]) and
    always opts out of distribution entirely. A runtime `anchor` has the next
    priority: an [x, y] scene-ref pin resolved by decompose replaces the
    facility's own geometric center. Surface pins use it as the
    `distribute_slots` center; container pins project it into the live support
    geom's local frame and clamp it inside the shelf. N objects sent to one pin
    fan out around it; with n=1 this collapses to the exact in-bounds pin point.
    Otherwise, an object-specific manifest `object_slot_points` entry has the
    next priority, followed by the generic `slot_points` list. XY-only points
    still derive z from the support surface, and requesting more objects than
    configured generic slots is a capacity error.
    `z_offset` stays a fallback for when the ray misses (surface case)."""
    regions = PLACEMENT_REGIONS if placement_regions is None else placement_regions
    if facility not in regions:
        raise ValueError(f"no placement region for facility '{facility}'")
    reg = regions[facility]
    fixed_slot = None
    if at is None and anchor is None:
        fixed_slot = (reg.get("object_slot_points") or {}).get(object_name)
        if fixed_slot is None:
            fixed_slot = _fixed_slot_point(facility, reg, slot, n)
    if fixed_slot is not None:
        at = fixed_slot
    if reg.get("kind", "surface") == "container":
        return _container_dest_point(
            rig, reg["interior_body"], q, slot, n, at,
            support_geom=reg.get("support_geom"),
            front_access=reg.get("access") == "front",
            front_target_offset=reg.get("front_target_offset", 0.10),
            slot_spread=reg.get("slot_spread", 0.70),
            slot_fracs=reg.get("slot_fracs"),
            front_target_max_fraction=reg.get(
                "front_target_max_fraction", 0.70),
            slot_center_frac=reg.get("slot_center_frac", 0.0),
            anchor=anchor)

    center = (
        np.asarray(reg["center"], dtype=float)
        if "center" in reg
        else rig.body_xy(reg["body"]) + [0, 0, reg["z_offset"]]
    )
    if at is not None:
        p = np.asarray(at, dtype=float)
        x, y = float(p[0]), float(p[1])
        z = float(p[2]) if len(p) >= 3 else None
    else:
        slot_center = center
        if anchor is not None:
            a = np.asarray(anchor, dtype=float)
            # Clamp the pin into the facility's own placement region so a
            # slightly-off pin (e.g. a click near the edge) still distributes
            # slots on the actual surface.
            clamped = np.clip(
                a - center[:2], -np.asarray(reg["half"]), np.asarray(reg["half"])
            ) + center[:2]
            slot_center = np.array([clamped[0], clamped[1], center[2]])
        slot_pt = distribute_slots(slot_center, reg["half"], n)[slot]
        x, y, z = float(slot_pt[0]), float(slot_pt[1]), None
    if z is None:
        s = rig.surface_z(x, y)
        z = s if s is not None else float(center[2])
    return np.array([x, y, z])


# ------------------------------------------------------ resting-object physics
# Grasp/carry stays kinematic (attach-to-gripper via FK, as above) — this is
# deliberately narrow: ONLY an object that has been placed and released
# becomes a real dynamic body, for as long as it sits resting somewhere,
# including through any later articulation step (Open/Close) that moves its
# container. That's the one case the kinematic "freeze at a fixed world pose"
# approach can't handle — a static destination (sink/counter) never needs
# this since nothing moves it again after placement.

def _settle_objects_physically(rig, q, object_poses, settle_time=0.4):
    """Jointly settle released objects under gravity/contact.

    ``object_poses`` is ``{obj_name: (pos, quat)}``. All objects share one
    MuJoCo rollout so object-object contacts are real; returning per-object
    frames also lets the generated place track keep every resting object in
    sync, rather than updating only the newly released one.

    Baselines the scene at the CURRENTLY COMPILED pose `q`, not qpos0 — a
    container's own fixture joint must already be at its open value at this
    point (the preceding Open* step wrote it into `q`), or the drop position
    (computed against the OPEN interior) has no floor under it at qpos0's
    CLOSED default and the object free-falls straight through to the room
    floor instead of settling on the container's floor.
    """
    m, d = rig.model, rig.data
    dt = float(m.opt.timestep)
    object_adrs = {
        obj_name: rig.jadr(_obj_joint(obj_name))
        for obj_name in object_poses
    }
    d.qpos[:] = q
    for obj_name, (pos, quat) in object_poses.items():
        obj_adr = object_adrs[obj_name]
        d.qpos[obj_adr:obj_adr + 3] = pos
        d.qpos[obj_adr + 3:obj_adr + 7] = quat
    d.qvel[:] = 0.0
    mujoco.mj_forward(m, d)

    n_steps = max(1, int(round(settle_time / dt)))
    sample_every = max(1, int(round((1.0 / FPS) / dt)))
    frames = {
        obj_name: [[*map(float, d.qpos[obj_adr:obj_adr + 3]),
                    *map(float, d.qpos[obj_adr + 3:obj_adr + 7])]]
        for obj_name, obj_adr in object_adrs.items()
    }
    for i in range(1, n_steps + 1):
        mujoco.mj_step(m, d)
        if i % sample_every == 0:
            for obj_name, obj_adr in object_adrs.items():
                frames[obj_name].append([
                    *map(float, d.qpos[obj_adr:obj_adr + 3]),
                    *map(float, d.qpos[obj_adr + 3:obj_adr + 7]),
                ])
    final_poses = {
        obj_name: (
            d.qpos[obj_adr:obj_adr + 3].copy(),
            d.qpos[obj_adr + 3:obj_adr + 7].copy(),
        )
        for obj_name, obj_adr in object_adrs.items()
    }
    return frames, final_poses


def _settle_object_physically(rig, q, obj_name, pos, quat, settle_time=0.4):
    """Backward-compatible single-object wrapper."""
    frames, final_poses = _settle_objects_physically(
        rig, q, {obj_name: (pos, quat)}, settle_time=settle_time)
    return frames[obj_name], final_poses[obj_name]


def _extend_track_with_settle_objects(track, object_frames):
    """Append a joint physical-settle tail for every resting object.

    Objects already resting in the facility may not yet be channels of this
    place track. Add them at their frame-0 pose for the authored motion, then
    append all jointly simulated poses so frontend playback matches the shared
    world committed by the compiler.
    """
    if not object_frames:
        return
    frame_count = len(next(iter(object_frames.values())))
    if frame_count <= 1:
        return
    authored_frames = len(track["time"])
    for obj_name, frames in object_frames.items():
        obj_jn = _obj_joint(obj_name)
        if obj_jn not in track["channels"]:
            track["channels"][obj_jn] = [
                list(frames[0]) for _ in range(authored_frames)]
    dt = 1.0 / FPS
    last_t = track["time"][-1]
    last_phase = track["phase"][-1]
    object_joints = {_obj_joint(name) for name in object_frames}
    last_vals = {
        jn: vals[-1]
        for jn, vals in track["channels"].items()
        if jn not in object_joints
    }
    for k in range(1, frame_count):
        track["time"].append(round(last_t + k * dt, 5))
        track["phase"].append(f"{last_phase}_settle")
        for jn, val in last_vals.items():
            track["channels"][jn].append(list(val))
        for obj_name, frames in object_frames.items():
            track["channels"][_obj_joint(obj_name)].append(frames[k])
    track["meta"]["n_frames"] = len(track["time"])
    track["meta"]["duration"] = track["time"][-1]


def _extend_track_with_settle(track, obj_name, frames):
    """Backward-compatible single-object wrapper."""
    _extend_track_with_settle_objects(track, {obj_name: frames})


def _lerp_track_channels(track, t):
    """All of `track`'s channel values linearly interpolated at time `t`
    (clamped to the track's own time range)."""
    times = track["time"]
    if t <= times[0]:
        return {jn: vals[0] for jn, vals in track["channels"].items()}
    if t >= times[-1]:
        return {jn: vals[-1] for jn, vals in track["channels"].items()}
    i = bisect.bisect_right(times, t) - 1
    i = max(0, min(i, len(times) - 2))
    t0, t1 = times[i], times[i + 1]
    a = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
    return {jn: [(1 - a) * v0 + a * v1 for v0, v1 in zip(vals[i], vals[i + 1])]
            for jn, vals in track["channels"].items()}


def _physics_replay_window(track):
    """Return (simulation cutoff, last meaningful fixture-motion time).

    Articulation tracks include robot approach/withdrawal around the actual
    fixture movement.  Once every declared fixture joint is stationary, those
    robot-only frames cannot drive an object resting inside the container.
    Preserve a short physics tail after the last meaningful fixture transition
    so contact-induced object velocity can settle before its pose is frozen for
    the remainder of the visual track.

    Missing or malformed fixture metadata deliberately falls back to the full
    track: trimming is an optimization, never a requirement for correctness.
    """
    times = track.get("time") or []
    if not times:
        return 0.0, 0.0
    full_duration = float(times[-1])
    fixture_joints = (track.get("meta") or {}).get("fixture_joints") or []
    if not _PHYSICS_REPLAY_TRIM or not fixture_joints:
        return full_duration, full_duration

    last_motion = None
    for joint_name in fixture_joints:
        values = (track.get("channels") or {}).get(joint_name)
        if values is None or len(values) != len(times):
            return full_duration, full_duration
        for index in range(1, len(times)):
            if abs(float(values[index][0]) - float(values[index - 1][0])) > (
                    PHYSICS_REPLAY_FIXTURE_MOTION_EPS):
                transition_end = float(times[index])
                last_motion = (
                    transition_end if last_motion is None
                    else max(last_motion, transition_end))

    # A declared fixture that never moves needs no contact replay beyond a
    # settling tail at frame zero.  Clamp to the authored duration either way.
    if last_motion is None:
        last_motion = float(times[0])
    cutoff = min(
        full_duration, last_motion + PHYSICS_REPLAY_SETTLE_TAIL_SEC)
    return cutoff, last_motion


def _placement_support_moves_with_fixture(rig, raw_track, placement_region):
    """Whether an articulation joint kinematically moves the placement floor.

    A drawer's interior floor is below its slide joint, so every object resting
    on it must participate in the articulation physics replay. A fridge or
    cabinet shelf is normally fixed while only sibling door bodies move; in
    that case replaying thousands of ``mj_step`` calls merely re-settles an
    already stationary object. Resolve this relationship from the model body
    tree instead of requiring per-scene facility annotations.

    Return ``None`` when the relationship cannot be proved. Callers must treat
    that as the conservative replay-required case.
    """
    fixture_joints = (raw_track.get("meta") or {}).get("fixture_joints") or []
    interior_body = (placement_region or {}).get("interior_body")
    if not fixture_joints or not interior_body:
        return None

    m = rig.model
    try:
        support_gid = _interior_floor_geom(
            m, interior_body,
            support_geom=(placement_region or {}).get("support_geom"))
    except ValueError:
        return None

    for joint_name in fixture_joints:
        joint_id = mujoco.mj_name2id(
            m, mujoco.mjtObj.mjOBJ_JOINT, joint_name)
        if joint_id < 0:
            return None
        joint_body = int(m.jnt_bodyid[joint_id])
        if support_gid in _body_subtree_geoms(m, joint_body):
            return True
    return False


def _freeze_resting_objects_in_track(raw_track, resting_objects):
    """Add constant object channels without running contact simulation."""
    augmented = dict(raw_track)
    augmented["meta"] = dict(raw_track.get("meta") or {})
    augmented["channels"] = dict(raw_track.get("channels") or {})
    frame_count = len(raw_track.get("time") or [])
    final_poses = {}
    for obj_name, (pos, quat) in resting_objects.items():
        pose = [*map(float, pos), *map(float, quat)]
        augmented["channels"][_obj_joint(obj_name)] = [
            list(pose) for _ in range(frame_count)]
        final_poses[obj_name] = (
            np.asarray(pos, dtype=float).copy(),
            np.asarray(quat, dtype=float).copy(),
        )
    augmented["meta"].update({
        "physics_replay_skipped": True,
        "physics_replay_skip_reason": "static_support",
        "physics_substeps": 0,
        "physics_object_count": len(resting_objects),
        "physics_objects": list(resting_objects),
    })
    return augmented, final_poses


def _rigid_follow_resting_objects(
        rig, q, raw_track, resting_objects, interior_body):
    """Carry scene-authored thin contents rigidly with their container box."""
    m, d = rig.model, rig.data
    support_id = mujoco.mj_name2id(
        m, mujoco.mjtObj.mjOBJ_BODY, interior_body)
    if support_id < 0:
        raise ValueError(f"container interior body not found: {interior_body!r}")

    d.qpos[:] = q
    mujoco.mj_forward(m, d)
    support_pos0 = d.xpos[support_id].copy()
    support_quat0 = d.xquat[support_id].copy()
    local_poses = {}
    for obj_name, (pos, quat) in resting_objects.items():
        local_poses[obj_name] = (
            quat_rot(quat_conj(support_quat0), np.asarray(pos) - support_pos0),
            quat_mul(quat_conj(support_quat0), np.asarray(quat)),
        )

    augmented = dict(raw_track)
    augmented["meta"] = dict(raw_track.get("meta") or {})
    augmented["channels"] = dict(raw_track.get("channels") or {})
    object_channels = {obj_name: [] for obj_name in resting_objects}
    frame_q = q.copy()
    for frame_index in range(len(raw_track.get("time") or [])):
        for joint_name, values in raw_track["channels"].items():
            adr = rig.jadr(joint_name)
            frame_q[adr:adr + len(values[frame_index])] = values[frame_index]
        d.qpos[:] = frame_q
        mujoco.mj_forward(m, d)
        support_pos = d.xpos[support_id].copy()
        support_quat = d.xquat[support_id].copy()
        for obj_name, (local_pos, local_quat) in local_poses.items():
            world_pos = support_pos + quat_rot(support_quat, local_pos)
            world_quat = quat_mul(support_quat, local_quat)
            object_channels[obj_name].append([
                *map(float, world_pos), *map(float, world_quat)
            ])

    final_poses = {}
    for obj_name, poses in object_channels.items():
        augmented["channels"][_obj_joint(obj_name)] = poses
        final = np.asarray(poses[-1], dtype=float)
        final_poses[obj_name] = (final[:3].copy(), final[3:7].copy())
    augmented["meta"].update({
        "physics_replay_skipped": True,
        "physics_replay_skip_reason": "scene_authored_rigid_container_contents",
        "physics_substeps": 0,
        "physics_object_count": len(resting_objects),
        "physics_objects": list(resting_objects),
        "rigid_follow_body": interior_body,
    })
    return augmented, final_poses


def _replay_or_freeze_resting_objects(
        rig, q, raw_track, resting_objects, placement_region):
    """Replay only when the articulated fixture moves the support surface."""
    support_moves = _placement_support_moves_with_fixture(
        rig, raw_track, placement_region)
    if support_moves is False:
        return _freeze_resting_objects_in_track(raw_track, resting_objects)
    return _replay_with_resting_objects(rig, q, raw_track, resting_objects)


def _replay_with_resting_objects(rig, q, raw_track, resting_objects):
    """Replay an articulation skill with resting objects jointly simulated.

    ``resting_objects`` is ``{obj_name: (pos, quat)}``. Every scripted channel
    in `raw_track`
    (robot joints + the fixture joint(s) it drives) follows its EXACT
    recorded relative motion — both position and a finite-difference
    velocity, so the contact solver sees the real closing/opening speed —
    while every resting object's free joint is left to the same mj_step contact
    solve. This preserves fixture-object and object-object interaction.

    Scripted joints are anchored to `q`'s CURRENT value and replay
    raw_track's motion as a DELTA from raw_track's own frame 0 — not
    raw_track's absolute values directly. Different recorded demos don't
    necessarily agree on the exact "how open is open" starting point (e.g.
    this scene's CloseDrawer_stack4 demo begins measurably short of where the
    OpenDrawer demo ends). Replaying absolute values would snap the fixture
    joint — and the container's whole interior geometry — at frame 0,
    instantly interpenetrating the resting object with the just-moved floor/
    walls and triggering a violent contact-resolution "pop" instead of a
    smooth push (this is exactly what was observed before this fix: a mug
    ~0.5m X jump in the first few substeps, while the drawer's own recorded
    value hadn't even started moving yet). Anchoring to `q` keeps the
    container's geometry continuous with wherever the previous step actually
    left it; this track's own recorded motion still fully drives it from there.

    Returns (augmented_track, final_resting_poses); `raw_track` itself is not
    mutated.
    """
    m, d = rig.model, rig.data
    model_dt = float(m.opt.timestep)
    dt = model_dt * PHYSICS_REPLAY_STEP_MULTIPLIER
    scripted_names = list(raw_track["channels"].keys())
    if any(len(raw_track["channels"][jn][0]) != 1 for jn in scripted_names):
        raise ValueError(
            "_replay_with_resting_objects expects only scalar (hinge/slide) "
            "scripted channels — got a multi-dof channel in this track"
        )
    scripted_jid = {
        jn: mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, jn)
        for jn in scripted_names
    }
    scripted_qadr = {jn: rig.jadr(jn) for jn in scripted_names}
    scripted_dofadr = {
        jn: int(m.jnt_dofadr[scripted_jid[jn]])
        for jn in scripted_names
    }
    cyclic = {
        jn for jn in scripted_names
        if int(m.jnt_type[scripted_jid[jn]]) == int(mujoco.mjtJoint.mjJNT_HINGE)
        and not bool(m.jnt_limited[scripted_jid[jn]])
    }
    object_adrs = {
        obj_name: rig.jadr(_obj_joint(obj_name))
        for obj_name in resting_objects
    }

    anchor = {jn: float(q[scripted_qadr[jn]]) for jn in scripted_names}
    frame0_raw = {jn: raw_track["channels"][jn][0][0] for jn in scripted_names}
    anchor_delta = {
        jn: (
            float(_wrap_pi(anchor[jn] - frame0_raw[jn]))
            if jn in cyclic
            else float(anchor[jn] - frame0_raw[jn])
        )
        for jn in scripted_names
    }
    # Dense scalar views used by the physics-rate inner loop below.  The old
    # path called _lerp_track_channels for every 2 ms step, which bisected the
    # same frame interval we are already iterating and allocated a full dict of
    # per-channel lists.  All scripted channels were validated scalar above, so
    # the exact same linear targets can be computed in one vector operation.
    scripted_qadr_array = np.asarray(
        [scripted_qadr[jn] for jn in scripted_names], dtype=int)
    scripted_dofadr_array = np.asarray(
        [scripted_dofadr[jn] for jn in scripted_names], dtype=int)
    scripted_values = np.asarray(
        [[frame[0] for frame in raw_track["channels"][jn]]
         for jn in scripted_names],
        dtype=float)
    anchor_delta_array = np.asarray(
        [anchor_delta[jn] for jn in scripted_names], dtype=float)
    cyclic_mask = np.asarray(
        [jn in cyclic for jn in scripted_names], dtype=bool)

    d.qpos[:] = q
    for obj_name, (resting_pos, resting_quat) in resting_objects.items():
        obj_adr = object_adrs[obj_name]
        d.qpos[obj_adr:obj_adr + 3] = resting_pos
        d.qpos[obj_adr + 3:obj_adr + 7] = resting_quat
    d.qvel[:] = 0.0
    mujoco.mj_forward(m, d)

    times = raw_track["time"]
    duration = times[-1] if times[-1] > 1e-9 else 1.0
    replay_cutoff, fixture_last_motion = _physics_replay_window(raw_track)

    def _target(jn, t, raw_val):
        # Every scripted channel — the fixture joint(s) AND the robot's own
        # base/arm/finger joints — follows raw_track's actual recorded motion
        # (the robot really replays "push drawer shut, withdraw hand, walk
        # away" instead of standing frozen). Full anchor correction at t=0
        # keeps continuity with wherever `q` currently is; linearly tapered to
        # ZERO by the end so every channel still finishes at the raw
        # recording's own absolute value (e.g. fully closed, or the demo's
        # original entry stance) rather than offset by however much the start
        # had to be corrected.
        blend = max(0.0, 1.0 - t / duration)
        return raw_val + anchor_delta[jn] * blend

    out_objects = {
        obj_name: [[*map(float, resting_pos), *map(float, resting_quat)]]
        for obj_name, (resting_pos, resting_quat) in resting_objects.items()
    }
    total_substeps = 0
    full_substeps = 0
    reference_substeps = 0
    m.opt.timestep = dt
    try:
        for i in range(len(times) - 1):
            t0, t1 = times[i], times[i + 1]
            span = max(t1 - t0, 1e-6)
            full_substeps += max(1, int(round(span / dt)))
            reference_substeps += max(1, int(round(span / model_dt)))
            simulated_end = min(float(t1), replay_cutoff)
            simulated_span = max(0.0, simulated_end - float(t0))
            nsub = (
                max(1, int(round(simulated_span / dt)))
                if simulated_span > 1e-9 else 0)
            total_substeps += nsub
            for s in range(1, nsub + 1):
                tt = t0 + (s / nsub) * simulated_span
                alpha = (tt - t0) / span
                targets = (
                    scripted_values[:, i]
                    + alpha * (
                        scripted_values[:, i + 1] - scripted_values[:, i])
                    + anchor_delta_array * max(0.0, 1.0 - tt / duration)
                )
                current = d.qpos[scripted_qadr_array].copy()
                if np.any(cyclic_mask):
                    # 0 and 2*pi are the same hinge pose. Keep cyclic targets on
                    # the branch nearest the current qpos so an equivalent yaw
                    # representation cannot inject a full spin.
                    targets[cyclic_mask] = (
                        current[cyclic_mask]
                        + _wrap_pi(
                            targets[cyclic_mask] - current[cyclic_mask]))
                d.qvel[scripted_dofadr_array] = (targets - current) / dt
                d.qpos[scripted_qadr_array] = targets
                mujoco.mj_step(m, d)
            for obj_name, obj_adr in object_adrs.items():
                out_objects[obj_name].append([
                    *map(float, d.qpos[obj_adr:obj_adr + 3]),
                    *map(float, d.qpos[obj_adr + 3:obj_adr + 7]),
                ])
    finally:
        # SceneRig instances are cached across compile requests. Never leak the
        # compile-only coarse timestep into placement settling or later plans.
        m.opt.timestep = model_dt

    final_poses = {
        obj_name: (
            d.qpos[obj_adr:obj_adr + 3].copy(),
            d.qpos[obj_adr + 3:obj_adr + 7].copy(),
        )
        for obj_name, obj_adr in object_adrs.items()
    }

    augmented = dict(raw_track)
    augmented["meta"] = dict(raw_track["meta"])
    augmented["meta"]["physics_substeps"] = total_substeps
    augmented["meta"]["physics_full_substeps"] = full_substeps
    augmented["meta"]["physics_reference_substeps"] = reference_substeps
    augmented["meta"]["physics_skipped_substeps"] = (
        full_substeps - total_substeps)
    augmented["meta"]["physics_dt"] = dt
    augmented["meta"]["physics_model_dt"] = model_dt
    augmented["meta"]["physics_step_multiplier"] = (
        PHYSICS_REPLAY_STEP_MULTIPLIER)
    augmented["meta"]["physics_sim_time_sec"] = round(total_substeps * dt, 4)
    augmented["meta"]["physics_full_sim_time_sec"] = round(
        full_substeps * dt, 4)
    augmented["meta"]["physics_fixture_last_motion_time_sec"] = round(
        fixture_last_motion, 4)
    augmented["meta"]["physics_replay_cutoff_time_sec"] = round(
        replay_cutoff, 4)
    augmented["meta"]["physics_settle_tail_sec"] = (
        PHYSICS_REPLAY_SETTLE_TAIL_SEC)
    augmented["meta"]["physics_object_count"] = len(resting_objects)
    augmented["meta"]["physics_objects"] = list(resting_objects)
    augmented["channels"] = {
        jn: [[_target(jn, t, v[0])] for v, t in zip(raw_track["channels"][jn], times)]
        for jn in scripted_names
    }
    for obj_name, frames in out_objects.items():
        augmented["channels"][_obj_joint(obj_name)] = frames
    return augmented, final_poses


def _replay_with_resting_object(
        rig, q, raw_track, obj_name, resting_pos, resting_quat):
    """Backward-compatible single-object wrapper."""
    augmented, final_poses = _replay_with_resting_objects(
        rig, q, raw_track, {obj_name: (resting_pos, resting_quat)})
    return augmented, final_poses[obj_name]


def _reverse_track(track, skill=None):
    """Time-reverse a pre-recorded track (channels + phase), recomputing a
    monotonic `time` array from the reversed frame-to-frame deltas. `skill`
    relabels meta.skill (e.g. the Close name) so a reversed Open displays as the
    Close it represents instead of keeping the Open's name.

    Used to play a Close skill as the EXACT reverse of its Open pair when a
    resting object needs the two ends to line up: an independently-recorded
    Close demo doesn't necessarily start where the Open demo left off (this
    scene's CloseDrawer_stack4 begins ~0.13 short of OpenDrawer's own last
    frame), which _replay_with_resting_object otherwise has to paper over
    with an anchor+taper correction. Reversing Open removes the mismatch
    entirely by construction — frame 0 becomes Open's own LAST frame (exactly
    where the container currently is) and the last frame becomes Open's own
    FIRST frame (its original closed state), so both ends are exact, not
    reconciled.
    """
    times = track["time"]
    diffs = [times[i + 1] - times[i] for i in range(len(times) - 1)]
    new_times = [0.0]
    for d in reversed(diffs):
        new_times.append(new_times[-1] + d)
    new_meta = dict(track["meta"])
    new_meta["duration"] = new_times[-1]
    if skill is not None:
        new_meta["skill"] = skill
    return {
        "meta": new_meta,
        "time": new_times,
        "phase": list(reversed(track["phase"])),
        "channels": {jn: list(reversed(vals)) for jn, vals in track["channels"].items()},
    }


def _apply_track_last_frame(rig, q, track):
    """Advance q to a pre-recorded track's final pose (for state threading)."""
    for jn, vals in track["channels"].items():
        try:
            a = rig.jadr(jn)
        except Exception:
            continue
        q[a:a + len(vals[-1])] = vals[-1]
    return q


def _track_base_trace(rig, track):
    """Every recorded chassis keyframe as ``[relative_time, world_x, world_y]``.

    Base translation joints are affine under a fixed robot mount, so derive the
    world-space basis once and transform all frames without an mj_forward call
    per frame.  Conflict clearance is a physical-base contract, so this must
    trace the yaw-joint anchor (``chassis_xy``), not the offset arm-mount origin
    (``base_xy``). The trace stays internal to compile/conflict detection; it is
    not serialized to the frontend or exposed to the conflict-resolution LLM.
    """
    robot = rig.robot
    channels = track.get("channels") or {}
    fwd = channels.get(f"mobilebase{robot}_joint_mobile_forward")
    side = channels.get(f"mobilebase{robot}_joint_mobile_side")
    times = track.get("time") or []
    if not fwd or not side or len(fwd) != len(side) or len(fwd) != len(times):
        return None

    basis = getattr(rig, "_conflict_chassis_xy_basis", None)
    if basis is None:
        q = rig.model.qpos0.copy()
        q[rig.FWD] = q[rig.SIDE] = q[rig.YAW] = 0.0
        origin = np.asarray(rig.chassis_xy(q), dtype=float)
        q[rig.FWD] = 1.0
        forward = np.asarray(rig.chassis_xy(q), dtype=float) - origin
        q[rig.FWD], q[rig.SIDE] = 0.0, 1.0
        lateral = np.asarray(rig.chassis_xy(q), dtype=float) - origin
        basis = (origin, forward, lateral)
        rig._conflict_chassis_xy_basis = basis
    origin, forward, lateral = basis

    return [
        [
            float(t),
            float((origin + float(f[0]) * forward + float(s[0]) * lateral)[0]),
            float((origin + float(f[0]) * forward + float(s[0]) * lateral)[1]),
        ]
        for t, f, s in zip(times, fwd, side)
    ]


def _skill_entry_standoff(rig, track):
    """The base pose a pre-recorded (non-parametric) skill starts from, as a
    {standoff_xy, face_xy} target. A navigate that precedes such a skill should
    drive here — position AND orientation — so playback doesn't jump when the
    skill's own base trajectory takes over. Reconstructs the skill's frame-0
    qpos, reads the base world XY, and derives the facing from the base's world
    forward direction at that pose."""
    q = rig.model.qpos0.copy()
    for jn, vals in track["channels"].items():
        try:
            a = rig.jadr(jn)
        except Exception:
            continue
        q[a:a + len(vals[0])] = vals[0]  # frame 0
    xy = np.asarray(rig.base_xy(q), dtype=float)
    # The base's world facing is YAW_joint + fwd_yaw, where fwd_yaw is the world
    # heading for +FWD at YAW=0 (the same basis place_base uses). FWD/SIDE are
    # translation axes, NOT the facing, so derive the heading from the yaw joint.
    z = rig.model.qpos0.copy()
    z[rig.YAW] = z[rig.FWD] = z[rig.SIDE] = 0.0
    b0 = np.asarray(rig.base_xy(z), dtype=float)
    z[rig.FWD] = 1.0
    fdir = np.asarray(rig.base_xy(z), dtype=float) - b0
    fwd_yaw = float(np.arctan2(fdir[1], fdir[0]))
    facing = float(q[rig.YAW]) + fwd_yaw
    face = xy + np.array([np.cos(facing), np.sin(facing)])
    return {"standoff_xy": xy, "face_xy": face}


def _skill_entry_pose(rig, q_cur, track):
    """Full qpos the pre-recorded `track` starts from — track's frame-0 values
    written over a COPY of `q_cur` (so joints the track doesn't script, e.g. a
    resting object's free joint, keep their current pose). This is the pose the
    robot must actually be in when the replay takes over, base AND arm."""
    q = q_cur.copy()
    for jn, vals in track["channels"].items():
        try:
            a = rig.jadr(jn)
        except Exception:
            continue
        q[a:a + len(vals[0])] = vals[0]
    return q


def _track_record_mount(track):
    """(x, y, yaw) mount the track's base channels were recorded against.

    Read from the track's own ``meta.recording.robot_mount`` — written by
    extract_skill_tracks at extraction time (or by migrate_track_mounts for
    legacy tracks). There is deliberately NO global fallback table: anchoring
    a replay to a mount from a different scene is exactly the bug that sent
    012 navigation targets outside the kitchen.
    """
    recording = (track.get("meta") or {}).get("recording") or {}
    mount = recording.get("robot_mount")
    if not mount:
        raise ValueError(
            f"track '{(track.get('meta') or {}).get('skill', '?')}' has no "
            "meta.recording.robot_mount; re-extract it with "
            "mujoco_skills.pipeline.extract_skill_tracks or stamp legacy "
            "tracks with mujoco_skills.pipeline.migrate_track_mounts"
        )
    return (
        float(mount["position"][0]),
        float(mount["position"][1]),
        float(mount["yaw"]),
    )

# Retargeting an articulation track runs mj_forward once per recorded frame and
# is repeated both by the navigate preview and the actual Open/Close replay.
# Cache only the immutable template.  Callers receive a deep copy because
# reverse/contact replay and state-threading code may augment track dictionaries
# in-place; sharing the cached object would leak one compile into the next.
_RETARGETED_SKILL_TRACK_CACHE = {}
_RETARGETED_SKILL_TRACK_CACHE_LOCK = threading.Lock()


def _clear_retargeted_skill_track_cache():
    """Clear the process-local articulation template cache (mainly for tests)."""
    with _RETARGETED_SKILL_TRACK_CACHE_LOCK:
        _RETARGETED_SKILL_TRACK_CACHE.clear()


def _retargeted_skill_track_cache_key(rig, path, reverse_skill):
    """Key a template by source revision, scene/robot and current mount pose.

    The RECORD mount lives inside the track file itself, so the file identity
    (path + mtime + size) already covers it.
    """
    source = Path(path).resolve()
    stat = source.stat()
    rbid = mujoco.mj_name2id(
        rig.model, mujoco.mjtObj.mjOBJ_BODY, f"robot{rig.robot}_base")
    mount = tuple(float(v) for v in np.concatenate((
        rig.model.body_pos[rbid], rig.model.body_quat[rbid])))
    return (
        str(rig.scene_path), rig.robot, mount,
        str(source), stat.st_mtime_ns, stat.st_size, reverse_skill,
    )


def _base_world_pose(rig, q):
    """World (x, y, yaw) of the robot's base body for qpos `q`."""
    rig.data.qpos[:] = q
    mujoco.mj_forward(rig.model, rig.data)
    xy = rig.data.xpos[rig.base_body][:2].copy()
    rot = rig.data.xmat[rig.base_body].reshape(3, 3)
    return float(xy[0]), float(xy[1]), float(np.arctan2(rot[1, 0], rot[0, 0]))


def _retarget_skill_base(rig, track):
    """Re-anchor a recorded skill's base channels to the demo's TRUE world base
    pose under the CURRENT mount.

    Recorded mobile-base joints (forward/side/yaw) are relative to the robot's
    mount body. Moving/rotating the mount (e.g. parking robots at safe homes)
    makes those joints replay at a mount-shifted world pose, far from the fixture
    — the arm then reaches into empty space. The fixture is fixed, so the demo's
    world base pose is invariant; we recover it by transforming each recorded
    frame's (current-mount) world pose by the record->current rigid delta
    `M_record ∘ M_current⁻¹` (the base-local kinematics cancel, so the delta is
    just between the two robot_base BODY poses), then re-solve base joints for the
    current mount. Arm/torso/gripper channels are untouched. Anchoring to the demo
    pose (not a generic standoff) matters where they differ — e.g. the drawers,
    whose standoff sits ~0.8 m from the demo base."""
    fwd_j = f"mobilebase{rig.robot}_joint_mobile_forward"
    side_j = f"mobilebase{rig.robot}_joint_mobile_side"
    yaw_j = f"mobilebase{rig.robot}_joint_mobile_yaw"
    channels = track["channels"]
    if fwd_j not in channels:
        return track
    record = _track_record_mount(track)

    rbid = mujoco.mj_name2id(
        rig.model, mujoco.mjtObj.mjOBJ_BODY, f"robot{rig.robot}_base")
    cp, cq = rig.model.body_pos[rbid], rig.model.body_quat[rbid]
    cur = (float(cp[0]), float(cp[1]), 2.0 * np.arctan2(float(cq[3]), float(cq[0])))
    dyaw = record[2] - cur[2]

    # identical mounts -> the recorded joints already replay at the true world
    # pose; skip the per-frame re-anchoring loop entirely.
    if (abs(record[0] - cur[0]) < 1e-9 and abs(record[1] - cur[1]) < 1e-9
            and abs(dyaw) < 1e-9):
        return track
    cos_d, sin_d = np.cos(dyaw), np.sin(dyaw)
    rot_delta = np.array([[cos_d, -sin_d], [sin_d, cos_d]])
    record_xy, cur_xy = np.array(record[:2]), np.array(cur[:2])

    fwd_out, side_out, yaw_out = [], [], []
    for i in range(len(channels[fwd_j])):
        q = rig.model.qpos0.copy()
        q[rig.FWD] = channels[fwd_j][i][0]
        q[rig.SIDE] = channels[side_j][i][0]
        q[rig.YAW] = channels[yaw_j][i][0]
        wx, wy, wyaw = _base_world_pose(rig, q)  # current-mount world of recorded joints
        demo_xy = record_xy + rot_delta @ (np.array([wx, wy]) - cur_xy)
        demo_yaw = wyaw + dyaw
        face = demo_xy + np.array([np.cos(demo_yaw), np.sin(demo_yaw)])
        qn = rig.model.qpos0.copy()
        rig.place_base(qn, demo_xy, face)
        fwd_out.append([float(qn[rig.FWD])])
        side_out.append([float(qn[rig.SIDE])])
        yaw_out.append([float(qn[rig.YAW])])
    channels[fwd_j], channels[side_j], channels[yaw_j] = fwd_out, side_out, yaw_out
    return track


def _load_retargeted_skill_track(
        rig, path, *, reverse_skill=None):
    path = Path(path)
    key = _retargeted_skill_track_cache_key(rig, path, reverse_skill)
    with _RETARGETED_SKILL_TRACK_CACHE_LOCK:
        template = _RETARGETED_SKILL_TRACK_CACHE.get(key)
        if template is None:
            track = json.loads(path.read_text("utf-8"))
            if reverse_skill is not None:
                track = _reverse_track(track, reverse_skill)
            template = _retarget_skill_base(rig, track)
            _RETARGETED_SKILL_TRACK_CACHE[key] = template
        return copy.deepcopy(template)


def gen_reposition_for_skill(rig, q_start, target_q, label="reposition"):
    """Bridge from `q_start` to a pre-recorded skill's frame-0 pose `target_q`:
    route the base over to the skill's recorded standoff (arm held ready), then
    reach the arm/fingers into the skill's exact entry pose. Used before an
    (empty-handed) articulation replay whose recorded entry pose differs from
    where the robot currently is — e.g. closing a drawer after a `place` left
    the robot at the placement standoff, not where the close demo stands.
    Without this the replay would either snap the base/arm to the demo pose or
    (with anchor+taper) drag them along an unrelated straight-line drift.

    Returns (track, target_q). Empty-handed only (no carried object)."""
    A = np.asarray(rig.base_xy(q_start), dtype=float)
    S = np.asarray(rig.base_xy(target_q), dtype=float)
    # facing of target_q's base (same derivation as _skill_entry_standoff)
    z = rig.model.qpos0.copy()
    z[rig.YAW] = z[rig.FWD] = z[rig.SIDE] = 0.0
    b0 = np.asarray(rig.base_xy(z), dtype=float)
    z[rig.FWD] = 1.0
    fdir = np.asarray(rig.base_xy(z), dtype=float) - b0
    fwd_yaw = float(np.arctan2(fdir[1], fdir[0]))
    facing = float(target_q[rig.YAW]) + fwd_yaw
    face = S + np.array([np.cos(facing), np.sin(facing)])

    nav_grid = ensure_navigation_grid(rig.scene_path)
    full_route = plan_grid_route(nav_grid, A, S)
    visit = full_route[1:]

    # travel with arm at ready, fingers held where they are (empty-handed/open)
    base_tmpl = q_start.copy()
    rig.apply_ready(base_tmpl)
    for a in rig.FINGERS:
        base_tmpl[a] = q_start[a]

    def unwrap(pose, ref):
        pose[rig.YAW] = ref[rig.YAW] + _wrap_pi(pose[rig.YAW] - ref[rig.YAW])

    poses = [base_tmpl.copy()]
    durs = []
    prev = A
    for wp in visit:
        rot = poses[-1].copy()
        rig.place_base(rot, prev, wp)
        unwrap(rot, poses[-1])
        durs.append(max(0.3, abs(rot[rig.YAW] - poses[-1][rig.YAW]) / YAW_SPEED))
        poses.append(rot)
        beyond = wp + (wp - prev)
        trans = rot.copy()
        rig.place_base(trans, wp, beyond)
        unwrap(trans, rot)
        durs.append(max(0.4, np.linalg.norm(wp - prev) / BASE_SPEED))
        poses.append(trans)
        prev = wp
    # rotate to the skill's entry facing, still arm-ready
    stand = poses[-1].copy()
    rig.place_base(stand, S, face)
    unwrap(stand, poses[-1])
    durs.append(max(0.3, abs(stand[rig.YAW] - poses[-1][rig.YAW]) / YAW_SPEED))
    poses.append(stand)
    # reach the arm/fingers (and settle the base exactly) into the demo entry pose
    poses.append(target_q.copy())
    durs.append(1.0)

    track = build_track(rig, label, poses, durs, None)
    track["meta"]["route"] = [
        [float(point[0]), float(point[1])] for point in full_route
    ]
    track["meta"]["via_points"] = []
    track["meta"]["face_xy"] = [float(face[0]), float(face[1])]
    return track, target_q.copy()


STANDOFF_MIN_DIST = 0.8   # two bases closer than this (m) at overlapping times conflict


CORE_OPS = {"navigate", "pick", "place", "reset", "wait"}

# Warm cache of SceneRig instances (model load is ~1s; reuse across requests).
# SceneRig.data is mutable; skill_service serializes every compile/standoff
# access through its MuJoCo execution lock, and every compile resets qpos.
_RIGS = {}


def get_rig(scene_xml, robot, ready=None, island=None):
    # Key by file identity (path + mtime + size), not path alone: a scene
    # re-exported in place must produce a fresh rig, and compiling scene A
    # then B then A again must never reuse a stale model.
    stat = Path(scene_xml).stat()
    key = (str(scene_xml), stat.st_mtime_ns, stat.st_size, robot)
    if key not in _RIGS:
        _RIGS[key] = SceneRig(scene_xml, robot=robot)
    rig = _RIGS[key]
    if ready is not None:
        rig.set_ready(ready)
    if island is not None:
        rig.set_island(island)
    return rig


GENERATED_TRACK_DIR = "_generated"
# Keep generated artifact names comfortably below Win32's legacy MAX_PATH
# once the repository, study, plan namespace, and robot directories are added.
# The schedule label remains descriptive; only its on-disk representation is
# shortened when necessary.
GENERATED_TRACK_FILENAME_STEM_LIMIT = 72


def generated_track_filename(label):
    """Return the stable on-disk filename for a generated schedule label.

    Physics-aware articulation labels intentionally include every object
    resting in a container (for example ``CloseFridge_with_apple_1_and_...``).
    That information is useful in the schedule and track metadata, but using
    it verbatim as a filename can exceed Windows' total-path limit. Preserve
    short labels for readability and compact only long labels, retaining a
    readable prefix plus a deterministic digest for collision resistance.
    """
    label = str(label)
    if len(label) <= GENERATED_TRACK_FILENAME_STEM_LIMIT:
        return f"{label}.track.json"
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:16]
    prefix_length = GENERATED_TRACK_FILENAME_STEM_LIMIT - len(digest) - 2
    prefix = label[:prefix_length].rstrip(" ._-") or "track"
    return f"{prefix}__{digest}.track.json"


def is_generated_track_item(item):
    """Whether a compiled item is a per-plan artifact rather than a canonical
    RoboCasa replay track.

    Keep this classification shared by the writer and HTTP service: generated
    items must be served from ``tracks/_generated`` and never be mistaken for
    source skills during a later manifest rebuild.
    """
    return bool(item.get("generated_track")) or item["label"].startswith(
        ("navigate_", "pick_", "place_", "reset_", "reposition_"))


def write_generated_tracks(result, tracks_dir, namespace=None):
    """Persist tracks that were actually computed by this compile (navigate/
    pick/place, plus any articulation replay augmented with a resting
    object's physics — see `generated_track` in compile_robot) under
    ``tracks/_generated/<namespace>/<robot>`` when a namespace is supplied,
    otherwise the legacy ``tracks/_generated/<robot>``. A plan-hash namespace
    keeps cached responses immutable: compiling another plan with the same
    labels cannot overwrite the first plan's files. Plain (unaugmented) tracks
    remain in the canonical ``tracks/<robot>`` source directories. Returns the
    count."""
    if namespace is not None:
        namespace = str(namespace)
        if not namespace or any(
                ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for ch in namespace):
            raise ValueError(f"invalid generated-track namespace {namespace!r}")
    n = 0
    for it in result["items"]:
        if is_generated_track_item(it):
            target_dir = Path(tracks_dir) / GENERATED_TRACK_DIR
            if namespace is not None:
                target_dir /= namespace
            target_dir /= it["robot"]
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / generated_track_filename(it["label"])).write_text(
                json.dumps(it["track"]), encoding="utf-8")
            n += 1
    return n


def flatten_tasks(plan):
    """Adapter: nested authoring plan {"tasks":[{task,robot,steps:[...]}]} ->
    flat {robot: [steps]}. Steps inherit the task's robot, get an auto id if
    absent, and carry the task label as `group` (for the UI). A plan already in
    flat form is returned unchanged."""
    if "tasks" not in plan:
        flat = json.loads(json.dumps(plan))
        author_order = 0
        for robot, steps in flat.items():
            for robot_order, step in enumerate(steps):
                # Normalize the lane identity into every representation. A
                # completed step already echoes this field, while authored and
                # frontend-nested steps historically did not; that mismatch
                # made completed-plan memo aliases miss on compound append.
                # Assignment (not setdefault) also prevents a stale step echo
                # from disagreeing with a task moved to another robot lane.
                step["robot"] = robot
                step.setdefault("id", f"{robot}#{robot_order}")
                step["_author_order"] = author_order
                step["_robot_order"] = robot_order
                author_order += 1
        return flat
    flat = {}
    author_order = 0
    for ti, task in enumerate(plan["tasks"]):
        robot = task["robot"]
        for si, step in enumerate(task["steps"]):
            s = dict(step)
            s["robot"] = robot
            s.setdefault("id", f"t{ti}s{si}")
            s.setdefault("group", task.get("task"))
            s.setdefault("robot_locked", bool(task.get("robot_locked", False)))
            deferred_close = task.get(
                "_compiler_v2_deferred_close_facility")
            if deferred_close is not None:
                s.setdefault(
                    "_compiler_v2_deferred_close_facility", deferred_close)
            s["_author_order"] = author_order
            s["_robot_order"] = len(flat.get(robot, []))
            author_order += 1
            flat.setdefault(robot, []).append(s)
    return flat


def _scene_initial_qpos(rig, manifest):
    """Return the scene state declared by the manifest, or MuJoCo qpos0."""
    keyframe = (manifest.get("scene") or {}).get("init_keyframe")
    if not keyframe:
        return rig.model.qpos0.copy()
    key = mujoco.mj_name2id(
        rig.model, mujoco.mjtObj.mjOBJ_KEY, str(keyframe))
    if key < 0:
        raise ValueError(
            f"scene init_keyframe '{keyframe}' does not exist in the model")
    return rig.model.key_qpos[key].copy()


def _ready_from_scene_initial(rig, manifest):
    """Return this robot's ready state from the scene init keyframe.

    A replay is not a safe ready-pose source when it was synthesized by time
    reversal: frame 0 is then the source skill's terminal manipulation pose.
    Generated tracks must instead begin from the session state they overlay.
    """
    q = _scene_initial_qpos(rig, manifest)
    return {
        "arm": [float(q[address]) for address in rig.ARM],
        "torso": float(q[rig.TORSO]),
        "fingers": [float(q[address]) for address in rig.FINGERS],
    }


def _new_robot_compile_context(rig, initial_q=None):
    q = (rig.model.qpos0 if initial_q is None else initial_q).copy()
    rig.apply_ready(q)
    rig.set_fingers(q, rig.finger_open)
    return {
        "q": q,
        "held": None,
        "off": None,
        "held_grasp_mode": "top_down",
        "held_place_seed": None,
        "label_counts": {},
        "resting": {},
    }


def _overlay_shared_world(rig, q, shared_world, held):
    """Merge non-robot scene state into one robot's scratch qpos.

    Each robot keeps its own base/arm/gripper state, while fixture joints and
    unheld object free joints come from the shared world.  A held object's pose
    remains owned by its carrying robot until `place` releases it.
    """
    if shared_world is None:
        return q
    world_q = shared_world["q"]
    for adr, width in shared_world["fixture_spans"]:
        q[adr:adr + width] = world_q[adr:adr + width]
    for obj, (adr, width) in shared_world["object_spans"].items():
        if obj != held:
            q[adr:adr + width] = world_q[adr:adr + width]
    rig.data.qpos[:] = q
    mujoco.mj_forward(rig.model, rig.data)
    return q


def _commit_shared_world(rig, q, shared_world, held):
    """Commit fixture and released-object effects produced by one step."""
    if shared_world is None:
        return
    world_q = shared_world["q"]
    for adr, width in shared_world["fixture_spans"]:
        world_q[adr:adr + width] = q[adr:adr + width]
    for obj, (adr, width) in shared_world["object_spans"].items():
        if obj != held:
            world_q[adr:adr + width] = q[adr:adr + width]


def compile_robot(rig, robot_name, steps, standoffs, tracks_dir, facilities,
                  close_to_open=None, reverse_always=None, context=None,
                  shared_world=None, next_step=None, placement_regions=None):
    """Compile one robot's ordered steps into track items, threading base pose +
    held object. Start times are assigned later by schedule(). Each step may
    carry an "id" (else auto) and "after": [id,...] cross-robot dependencies.

    `reverse_always` is the set of Close skills that should ALWAYS play as the
    time-reverse of their Open pair (slide drawers) — not just when a resting
    object forces it — so open/close ends line up exactly with no start-point
    mismatch. Hinged doors (fridge/cabinet) are left out of this set."""
    context = context or _new_robot_compile_context(rig)
    q = context["q"].copy()
    held = context["held"]
    off = context["off"]
    held_grasp_mode = context["held_grasp_mode"]
    held_place_seed = context["held_place_seed"]
    items = []
    # Generated tracks are persisted by label.  A plan may revisit the same
    # target (e.g. navigate fridge before both apple and milk placement), so
    # labels must be unique within this compilation; otherwise the later file
    # overwrites the earlier one and playback silently substitutes a future
    # trajectory at the first visit.
    label_counts = context["label_counts"]
    close_to_open = close_to_open or {}
    reverse_always = reverse_always or set()
    placement_regions = (
        PLACEMENT_REGIONS if placement_regions is None else placement_regions)
    # facility -> {obj_name: (pos, quat)}: all objects physically resting in a
    # container. Cleared per object when re-picked.
    resting: dict[str, dict[str, tuple]] = (
        shared_world["resting"] if shared_world is not None
        else context["resting"])

    for i, step in enumerate(steps):
        step_started = time.perf_counter()
        q = _overlay_shared_world(rig, q, shared_world, held)
        order = int(step.get("_robot_order", i))
        op = step["op"]
        facility = obj = None
        place_xyz = None
        navigate_face_xy = None
        pin_standoff_used = False
        generated_track = op in ("navigate", "pick", "place", "reset")
        if op == "navigate":
            tgt = step["target"]
            carry = _carry(rig, held, off) if held else None
            # If the next step is a non-parametric articulation skill (a RoboCasa
            # replay), drive to that skill's own entry base pose (xy + facing)
            # instead of the generic standoff, so the base doesn't jump when the
            # pre-recorded track takes over.
            nxt = (
                steps[i + 1] if i + 1 < len(steps)
                else next_step)
            dest = None
            final_q = None
            following_skill = None
            if nxt and nxt["op"] not in CORE_OPS:
                following_skill = nxt.get("name", nxt["op"])
                following_facility = facilities.get(following_skill)
                preview_name = following_skill
                preview_reverse = False
                if (following_skill in close_to_open
                        and (following_skill in reverse_always
                             or following_facility in resting)):
                    preview_name = close_to_open[following_skill]
                    preview_reverse = True
                spath = Path(tracks_dir) / robot_name / f"{preview_name}.track.json"
                if spath.exists():
                    # Same mount re-anchor as the following skill replay.
                    entry_track = _load_retargeted_skill_track(
                        rig, spath,
                        reverse_skill=(following_skill if preview_reverse else None))
                    dest = _skill_entry_standoff(rig, entry_track)
                    final_q = _skill_entry_pose(rig, q, entry_track)
            if dest is None and tgt is not None:
                pinned_dest = _pinned_surface_standoff(
                    rig, tgt, nxt, q, placement_regions,
                    navigate_step=step)
                if pinned_dest is not None:
                    dest = pinned_dest
                    pin_standoff_used = True
            if dest is None and tgt is None:
                # No named target at all (e.g. insert_go_to's rest-point leg,
                # design doc §3c) — the step must supply its own standoff +
                # facing directly; there's no standoffs[] entry to fall back on.
                ov = step.get("standoff")
                if ov is None or len(ov) < 2:
                    raise ValueError(
                        "navigate step has no 'target' and no explicit "
                        "'standoff' xy to route to")
                if step.get("final_yaw") == "home":
                    # Keep the completed step's facing meaningful as well as
                    # making normal final alignment rotate to qpos0 yaw. A
                    # go_to_rest step intentionally has no named target, so
                    # falling back to face_xy == standoff would encode a zero
                    # vector. Unlike arrival_yaw, this path does not rotate
                    # before the final translation.
                    face = (
                        np.asarray(ov[:2], dtype=float)
                        + rig.base_forward(rig.model.qpos0)
                    )
                else:
                    face = step.get("face_xy", ov)
                dest = {"standoff_xy": [float(ov[0]), float(ov[1])],
                        "face_xy": [float(face[0]), float(face[1])]}
            elif dest is None:
                # A user-dragged standoff overrides only the dwell XY; keep facing
                # the target. NOT reachable in the articulation-skill branch above,
                # so a navigate feeding an Open/Close replay stays pinned to the
                # replay's recorded entry pose (its standoff is not authorable).
                base = standoffs[tgt]
                ov = step.get("standoff")
                if ov is not None and len(ov) >= 2:
                    dest = {"standoff_xy": [float(ov[0]), float(ov[1])],
                            "face_xy": base["face_xy"]}
                else:
                    dest = base
            navigate_face_xy = [
                round(float(dest["face_xy"][0]), 6),
                round(float(dest["face_xy"][1]), 6),
            ]
            nav_label = f"navigate_{tgt or 'rest'}"
            if following_skill is not None:
                nav_label += f"_for_{following_skill}"
            tr, q = gen_navigate(
                rig, q, dest, carrying=carry, label=nav_label,
                via_points=step.get("via_points"),
                waypoints=(
                    step.get("waypoints")
                    if step.get("via_points") is None else None),
                preserve_yaw=step.get("preserve_yaw", False),
                final_q=final_q,
                align_final_yaw=step.get("align_final_yaw", True),
                arrival_yaw=(
                    rig.model.qpos0[rig.YAW]
                    if step.get("arrival_yaw") == "home"
                    else None),
                standoff_is_chassis=tgt is None)
            facility = tgt if tgt in facilities and facilities[tgt] == tgt else None
            obj = held
        elif op == "pick":
            obj = step["object"]
            held_by = (
                shared_world["held_by"] if shared_world is not None else {})
            owner = held_by.get(obj)
            if owner is not None and owner != robot_name:
                raise ValueError(
                    f"object '{obj}' is already held by {owner}; "
                    f"{robot_name} cannot pick it")
            pick_grasp_mode = step.get("grasp_mode", "top_down")
            return_to_ready = step.get("return_to_ready", True)
            if pick_grasp_mode == "reachin":
                candidate_regions = [
                    (name, region)
                    for name, region in placement_regions.items()
                    if region.get("access") == "front"
                    and obj in (region.get("object_slot_points") or {})
                ]
                if len(candidate_regions) != 1:
                    raise ValueError(
                        f"reachin pick for '{obj}' requires exactly "
                        "one front-access facility with an object_slot_points "
                        f"entry; found {[name for name, _ in candidate_regions]}")
                _, region = candidate_regions[0]
                tr, q, off = gen_pick_reachin(
                    rig, q, obj, region["interior_body"],
                    state_source=region.get("state_source", "current"),
                    support_geom=region.get("support_geom"),
                    front_distance=region.get("front_distance"),
                    ik_eef_tolerance=region.get("ik_eef_tolerance", 0.012),
                    randomize_preinsert_first=region.get(
                        "randomize_preinsert_first", False),
                    ik_top_down=region.get("ik_top_down", False),
                    rrt_horizontal_ingress=region.get(
                        "rrt_horizontal_ingress", False),
                    reachin_lift_height=region.get("reachin_lift_height"),
                    cartesian_rrt_fallback=region.get(
                        "cartesian_rrt_fallback", False),
                    nearby_rrt_fallback=region.get(
                        "nearby_rrt_fallback", False),
                    grasp_offset=step.get("grasp_offset"),
                    calibrated_track_path=(
                        Path(tracks_dir) / robot_name
                        / f"pick_{obj}_reachin.track.json"))
            else:
                tr, q, off = gen_pick(
                    rig, q, obj, grasp_mode=pick_grasp_mode,
                    return_to_ready=return_to_ready,
                    grasp_offset=step.get("grasp_offset"),
                    post_grasp_lift=step.get("post_grasp_lift", 0.0),
                    ready_torso=step.get("ready_torso"))
            # Once a horizontal pick has returned to the canonical ready arm,
            # its actual carry frame is no longer the side-grasp orientation.
            # Let reach-in placement solve position-first from that real ready
            # frame instead of incorrectly locking the old horizontal wrist
            # orientation (which drives link7 into the upper shelf).
            held_grasp_mode = (
                "ready"
                if (pick_grasp_mode == "reachin"
                    or (pick_grasp_mode == "horizontal" and return_to_ready))
                else pick_grasp_mode
            )
            held_place_seed = (
                tr["meta"].get("horizontal_pregrasp_arm_seed")
                if held_grasp_mode == "ready"
                else None
            )
            held = obj
            if shared_world is not None:
                shared_world["held_by"][obj] = robot_name
            # picking a resting object back up hands it back to the
            # kinematic gripper-attach path; it's no longer resting anywhere.
            for resting_facility in list(resting):
                resting[resting_facility].pop(obj, None)
                if not resting[resting_facility]:
                    del resting[resting_facility]
        elif op == "place":
            obj, facility = step["object"], step["dest"]
            if held != obj:
                raise ValueError(
                    f"{robot_name} cannot place '{obj}'; "
                    f"currently holding {held!r}")
            # Select the placement motion generator by the facility's `access`
            # (top-down vs Phase B's collision-checked horizontal reach-in).
            region = placement_regions.get(facility, {})
            access = region.get("access", "top")
            dpt = dest_point(rig, facility, q, step.get("_slot", 0),
                             step.get("_slot_count", 1), step.get("at"),
                             step.get("at_anchor"), placement_regions,
                             object_name=obj)
            # 6 decimals (micrometres), not 3 (millimetres): `place_xyz` is
            # echoed into completed_step["at"], which a later compile can
            # feed straight back into dest_point's `at` override (see
            # below). gen_place itself always uses the unrounded `dpt`, so
            # this echo is purely for the round-trip / UI, but D7b's
            # completed-step cache alias (see the D7 step-memo comment block
            # above _STEP_MEMO) treats "compiled from the authored step" and
            # "compiled from that step's own completed form" as
            # interchangeable — a hit can serve the AUTHORED (unrounded)
            # geometry for a request carrying this ROUNDED echo. At 3
            # decimals that mismatch was large enough to perturb the IK's
            # redundant wrist DOF into a measurably different (still valid,
            # still within tolerance) joint solution; 6 decimals shrinks the
            # gap far enough below the IK's own tol=4e-3 convergence
            # tolerance that the two solve to the same result in practice.
            place_xyz = [round(float(v), 6) for v in dpt]
            if access == "top":
                tr, q = gen_place(
                    rig, q, obj, dpt, off, label=f"place_{obj}_{facility}")
            elif access == "front":
                tr, q = gen_place_reachin(
                    rig, q, obj, region["interior_body"], off,
                    label=f"place_{obj}_{facility}", dest_xyz=dpt,
                    grasp_mode=held_grasp_mode,
                    goal_arm_seed=held_place_seed,
                    state_source=region.get("state_source", "study_init"),
                    support_geom=region.get("support_geom"),
                    front_distance=region.get("front_distance"),
                    ik_eef_tolerance=region.get("ik_eef_tolerance", 0.012),
                    randomize_preinsert_first=region.get(
                        "randomize_preinsert_first", False),
                    ik_top_down=region.get("ik_top_down", False),
                     rrt_horizontal_ingress=region.get(
                         "rrt_horizontal_ingress", False),
                     reachin_lift_height=region.get("reachin_lift_height"),
                     simple_ingress=region.get("simple_ingress", True),
                     release_clearance=region.get("release_clearance", 0.01),
                     cartesian_rrt_fallback=region.get(
                         "cartesian_rrt_fallback", False),
                     nearby_rrt_fallback=region.get(
                         "nearby_rrt_fallback", False))
            else:
                raise ValueError(
                    f"unknown place access '{access}' for facility '{facility}'")
            held, off, held_grasp_mode, held_place_seed = (
                None, None, "top_down", None)
            if shared_world is not None:
                shared_world["held_by"].pop(obj, None)
            if placement_regions.get(facility, {}).get("kind") == "container":
                # Narrow physics scope: only a placed-and-released object
                # becomes a real dynamic body. The gripper-drop pose above is
                # a kinematic estimate; physically settling it here confirms
                # (rather than assumes) it lands on the container's actual
                # floor, and gives `resting` a real, contact-consistent pose
                # to carry forward into any later Open/Close on this facility.
                last = tr["channels"][_obj_joint(obj)][-1]
                settling_objects = dict(resting.get(facility, {}))
                settling_objects[obj] = (
                    np.asarray(last[:3], dtype=float),
                    np.asarray(last[3:7], dtype=float),
                )
                frames, settled_objects = _settle_objects_physically(
                    rig, q, settling_objects)
                _extend_track_with_settle_objects(tr, frames)
                resting[facility] = settled_objects
                for resting_obj, (settled_pos, settled_quat) in (
                        settled_objects.items()):
                    radr = rig.jadr(_obj_joint(resting_obj))
                    q[radr:radr + 7] = [*settled_pos, *settled_quat]
        elif op == "reset":
            if held is not None:
                raise ValueError(
                    f"reset requires an empty gripper; currently holding '{held}'")
            # Keep physically settled container objects in the scratch qpos so
            # the reset collision checker sees the same scene as playback.
            for facility_objects in resting.values():
                for resting_obj, (rpos, rquat) in facility_objects.items():
                    radr = rig.jadr(_obj_joint(resting_obj))
                    q[radr:radr + 7] = [*rpos, *rquat]
            tr, q = gen_reset(
                rig, q,
                retreat=step.get("retreat", RESET_RETREAT_DEFAULT),
                preserve_yaw=step.get("preserve_yaw", True),
                label=f"reset_ready_{order}")
        elif op == "wait":
            carry = _carry(rig, held, off) if held else None
            tr = gen_wait(rig, q, step["duration"], carrying=carry)
            obj = held
        else:  # articulation: op is a pre-recorded skill name (or op:"skill",name:)
            name = step.get("name", op)
            facility = facilities.get(name)
            reversed_open = False
            if name in close_to_open and (name in reverse_always or facility in resting):
                # Play this Close as the exact time-reverse of its Open pair
                # instead of the independently-recorded Close file — removes
                # the open/close demo start-point mismatch entirely (see
                # _reverse_track), which matters once a resting object is
                # involved (an unreversed Close needed an anchor+taper
                # correction to paper over that mismatch; reversing Open makes
                # the correction unnecessary because both ends already agree
                # by construction).
                open_path = Path(tracks_dir) / robot_name / f"{close_to_open[name]}.track.json"
                if open_path.exists():
                    tr = _load_retargeted_skill_track(
                        rig, open_path, reverse_skill=name)
                    reversed_open = True
            if not reversed_open:
                path = Path(tracks_dir) / robot_name / f"{name}.track.json"
                if not path.exists():
                    raise ValueError(f"unknown op / no track for '{op}' (robot {robot_name})")
                tr = _load_retargeted_skill_track(rig, path)
            # The loader above re-anchors the recorded mount-relative
            # base to the demo's true world pose for the current mount.
            # The canonical file on disk keeps the original mount-relative base,
            # so serve THIS re-anchored copy from _generated instead.
            generated_track = True
            # Before replaying a pre-recorded skill, make sure the robot is
            # actually standing where that demo starts. After a `place`, the
            # robot sits at the placement standoff, NOT where the (reversed)
            # close demo stands — replaying from the wrong pose otherwise drags
            # the base/arm through an unrelated straight-line drift (the anchor+
            # taper in _replay_with_resting_object papering over a ~0.5m gap).
            # Bridge to the demo's frame-0 pose with a real reposition first, so
            # by the time the replay runs the robot is already there and the
            # anchor correction collapses to a no-op. Empty-handed only (a
            # carried object would need to ride the bridge), and only when the
            # gap is real (a skill already positioned needs no reposition).
            if held is None:
                entry_q = _skill_entry_pose(rig, q, tr)
                gap = float(np.linalg.norm(
                    np.asarray(rig.base_xy(q)) - np.asarray(rig.base_xy(entry_q))))
                if gap > 0.05:
                    btr, q = gen_reposition_for_skill(
                        rig, q, entry_q, label=f"reposition_{name}")
                    # 6 decimals, not 3 — see the place_xyz comment above;
                    # this becomes the synthetic reposition step's own
                    # completed_step["standoff"], which a resubmitted
                    # "completed" plan feeds back as a real navigate override.
                    bxy_b = [round(float(v), 6) for v in rig.base_xy(q)]
                    rid = f"{robot_name}#reposition{order}"
                    items.append({
                        "id": rid, "after": list(step.get("after", [])),
                        "robot": robot_name,
                        "order": order - 0.5, "label": btr["meta"]["skill"],
                        "track": btr, "duration": round(btr["meta"]["duration"], 3),
                        "group": step.get("group"), "facility": None,
                        "object": None, "place_xyz": None, "base_xy": bxy_b,
                        "base_trace": _track_base_trace(rig, btr),
                        "completed_step": {
                            "id": rid, "robot": robot_name, "op": "navigate",
                            "group": step.get("group"), "standoff": bxy_b,
                            "via_points": btr["meta"].get("via_points", []),
                            "route": btr["meta"].get("route"),
                        },
                        "generated_track": True,
                    })
            if facility in resting:
                # An object is physically resting in this facility (placed by
                # an earlier step). If the articulation moves its support
                # surface (a drawer), replay with real contact so it travels
                # with the container. If only a sibling door moves (a fridge
                # or cabinet), keep the already-settled world pose constant.
                facility_objects = resting[facility]
                placement_region = placement_regions.get(facility, {})
                if (facility in shared_world.get(
                        "initial_resting_facilities", set())
                        and placement_region.get("interior_body")):
                    tr, settled_objects = _rigid_follow_resting_objects(
                        rig, q, tr, facility_objects,
                        placement_region["interior_body"])
                else:
                    tr, settled_objects = _replay_or_freeze_resting_objects(
                        rig, q, tr, facility_objects, placement_region)
                resting[facility] = settled_objects
                # Give this augmented replay its OWN label/track file — NOT
                # the canonical skill name. `name`.track.json on disk is the
                # raw, object-unaware extracted demo, shared across every
                # compile; overwriting it here would leak this one
                # task/object's augmented motion into every OTHER plan that
                # later replays the same skill (e.g. with no object resting,
                # or a different one). write_generated_tracks below persists
                # this distinct file instead of (over)writing the shared one.
                tr = dict(tr)
                tr["meta"] = dict(tr["meta"])
                object_suffix = "_and_".join(facility_objects)
                tr["meta"]["skill"] = f"{name}_with_{object_suffix}"
                generated_track = True
            q = _apply_track_last_frame(rig, q, tr)

        _commit_shared_world(rig, q, shared_world, held)
        bxy = rig.base_xy(q)  # where the robot dwells during this item
        # 6 decimals, not 3 — see the place_xyz comment above: this becomes
        # completed_step["standoff"] for navigate/pick/place/reset, which a
        # resubmitted "completed" plan's navigate can feed straight back as
        # an explicit standoff override (compile_robot's navigate branch,
        # `ov = step.get("standoff")`).
        base_xy = [round(float(bxy[0]), 6), round(float(bxy[1]), 6)]

        # backend-completed copy of the authored step: spatial defaults filled
        # in (standoff / route / drop point) so the UI/LLM can inspect and
        # override them; user-provided overrides are preserved.
        cstep = dict(step)
        cstep["id"] = step.get("id", f"{robot_name}#{i}")
        cstep["robot"] = robot_name
        if op in ("navigate", "pick", "place", "reset"):
            cstep["standoff"] = base_xy
        if op == "navigate":
            if tgt is None:
                # Compiler-generated go-away/rest points live in navigation
                # space and therefore describe the physical chassis center.
                # Preserve that coordinate contract across completed-plan
                # round trips instead of echoing the offset arm mount.
                chassis = rig.chassis_xy(q)
                cstep["standoff"] = [
                    round(float(chassis[0]), 6),
                    round(float(chassis[1]), 6),
                ]
            legacy_vias = cstep.pop("waypoints", None)
            if "via_points" not in cstep and legacy_vias is not None:
                cstep["via_points"] = legacy_vias
            cstep.setdefault("via_points", tr["meta"].get("via_points", []))
            cstep["route"] = tr["meta"].get("route")
            cstep["face_xy"] = navigate_face_xy
            if pin_standoff_used:
                cstep["standoff_source"] = "pin"
        if op == "place":
            cstep["at"] = place_xyz
            # Reference used to decide whether a later scene drag moved the
            # placement far enough to require a new navigation stance. Reset
            # it after every successful solve so the threshold is local to the
            # latest compiled placement, not the original authoring default.
            cstep["standoff_at"] = place_xyz[:2]
        if op == "reset":
            cstep["retreat"] = tr["meta"]["retreat_distance"]
            cstep["preserve_yaw"] = tr["meta"]["preserve_yaw"]

        base_label = tr["meta"]["skill"]
        label_count = label_counts.get(base_label, 0) + 1
        label_counts[base_label] = label_count
        if label_count > 1:
            tr["meta"]["skill"] = f"{base_label}__{label_count}"

        step_elapsed = time.perf_counter() - step_started
        detail = _profile_detail(op, tr)
        _profile_log(
            f"{robot_name} step {order:>2} {op:9s} {tr['meta']['skill']:26s} "
            f"compile={step_elapsed:6.2f}s"
            + (f"  {detail}" if detail else ""))
        items.append({
            "id": cstep["id"],
            "after": list(step.get("after", [])),
            "robot": robot_name, "order": order,
            "label": tr["meta"]["skill"], "track": tr,
            "duration": round(tr["meta"]["duration"], 3),
            "group": step.get("group"),
            "facility": facility, "object": obj, "place_xyz": place_xyz,
            "base_xy": base_xy, "base_trace": _track_base_trace(rig, tr),
            "completed_step": cstep,
            "generated_track": generated_track,
            "compile_time_sec": round(step_elapsed, 3),
        })
    context.update({
        "q": q,
        "held": held,
        "off": off,
        "held_grasp_mode": held_grasp_mode,
        "held_place_seed": held_place_seed,
        "resting": resting,
    })
    return items


def _find_dependency_cycle(dependencies):
    """Return one deterministic waits-for cycle, including its repeated end.

    ``dependencies[node]`` contains the nodes that ``node`` must wait for, so
    adjacent IDs in the returned path can be read as "left waits for right".
    Dependencies outside the supplied graph are ignored; their validation is
    handled by the caller that owns the plan schema.
    """
    state = {}
    stack = []
    stack_index = {}

    def visit(node):
        state[node] = 1
        stack_index[node] = len(stack)
        stack.append(node)
        for dependency in sorted(dependencies.get(node, ())):
            if dependency not in dependencies:
                continue
            dependency_state = state.get(dependency, 0)
            if dependency_state == 0:
                cycle = visit(dependency)
                if cycle is not None:
                    return cycle
            elif dependency_state == 1:
                return stack[stack_index[dependency]:] + [dependency]
        stack.pop()
        stack_index.pop(node)
        state[node] = 2
        return None

    for node in sorted(dependencies):
        if state.get(node, 0) == 0:
            cycle = visit(node)
            if cycle is not None:
                return cycle
    return None


def schedule(items):
    """Assign each item a start time = max(previous step on same robot, all
    `after` dependencies) via fixpoint relaxation. Raises on dependency cycle."""
    id2 = {it["id"]: it for it in items}
    by_robot = {}
    for it in items:
        by_robot.setdefault(it["robot"], []).append(it)
    for lst in by_robot.values():
        lst.sort(key=lambda x: x["order"])
    for it in items:
        it["start"] = 0.0
    for _ in range(len(items) + 3):
        changed = False
        for lst in by_robot.values():
            for k, it in enumerate(lst):
                s = lst[k - 1]["start"] + lst[k - 1]["duration"] if k > 0 else 0.0
                for dep in it["after"]:
                    if dep in id2:
                        s = max(s, id2[dep]["start"] + id2[dep]["duration"])
                if abs(s - it["start"]) > 1e-9:
                    it["start"] = round(s, 3)
                    changed = True
        if not changed:
            break
    else:
        dependencies = {
            item["id"]: set(item.get("after", [])) for item in items
        }
        for robot_items in by_robot.values():
            for previous, current in zip(robot_items, robot_items[1:]):
                dependencies[current["id"]].add(previous["id"])
        cycle = _find_dependency_cycle(dependencies)
        if cycle is not None:
            raise ValueError(
                "dependency cycle in schedule (each step waits for the next): "
                + " -> ".join(cycle))
        raise ValueError("non-convergent schedule without a dependency cycle")
    return items


TIME_OVERLAP_EPS = 1e-9


def _overlap(a, b):
    """Whether two half-open time intervals overlap beyond float noise."""
    return (
        a["start"] < b["start"] + b["duration"] - TIME_OVERLAP_EPS
        and b["start"] < a["start"] + a["duration"] - TIME_OVERLAP_EPS
    )


OBJ_MIN_DIST = 0.12   # two placed objects closer than this (m) will collide


def _fallback_item_base_trace(item, start_xy):
    """Continuous fallback for synthetic/legacy items without a track trace."""
    start = float(item["start"])
    duration = max(float(item["duration"]), 0.0)
    completed = item.get("completed_step") or {}
    route = completed.get("route") or []
    if route:
        poly = [np.asarray(point, dtype=float) for point in route]
    else:
        poly = [np.asarray(start_xy, dtype=float)]
        legacy = completed.get("waypoints") or completed.get("via_points") or []
        poly.extend(np.asarray(point, dtype=float) for point in legacy)
        poly.append(np.asarray(item["base_xy"], dtype=float))
    lengths = [
        float(np.linalg.norm(poly[index + 1] - poly[index]))
        for index in range(len(poly) - 1)
    ]
    total = sum(lengths)
    if total < 1e-9:
        point = poly[-1]
        return [(start, point), (start + duration, point)]
    points = [(start, poly[0])]
    elapsed = 0.0
    for index, length in enumerate(lengths):
        elapsed += length
        points.append((start + duration * elapsed / total, poly[index + 1]))
    return points


def _item_base_trace(item, start_xy):
    """Absolute-time base trace, preserving every compiled track keyframe."""
    raw = item.get("base_trace")
    if not raw:
        return _fallback_item_base_trace(item, start_xy)
    start = float(item["start"])
    duration = max(float(item["duration"]), 0.0)
    raw_start, raw_end = float(raw[0][0]), float(raw[-1][0])
    raw_span = raw_end - raw_start
    points = []
    for frame in raw:
        fraction = (
            (float(frame[0]) - raw_start) / raw_span
            if raw_span > 1e-12 else 0.0
        )
        absolute_time = start + duration * min(1.0, max(0.0, fraction))
        point = np.asarray(frame[1:3], dtype=float)
        if points and abs(absolute_time - points[-1][0]) < 1e-12:
            points[-1] = (absolute_time, point)
        else:
            points.append((absolute_time, point))
    if len(points) == 1:
        points.append((start + duration, points[0][1]))
    return points


def _base_occupancy_segments(items, unfinished_robots=None):
    """Cover every robot's full ``[0, makespan]`` base occupancy.

    Active items use every track keyframe. Idle gaps hold the prior endpoint.
    A trailing gap is terminal only after that robot's whole program ends;
    ``unfinished_robots`` marks incremental frontiers that still have future
    steps and therefore remain ordinary inter-step dwells.
    """
    unfinished_robots = set(unfinished_robots or ())
    makespan = max(
        (float(item["start"]) + float(item["duration"]) for item in items),
        default=0.0,
    )
    by_robot = {}
    for item in items:
        by_robot.setdefault(item["robot"], []).append(item)
    result = {}
    for robot, robot_items in by_robot.items():
        robot_items.sort(key=lambda item: item["start"])
        segments = []
        previous_item = None
        previous_xy = np.asarray(robot_items[0]["base_xy"], dtype=float)
        cursor = 0.0
        for item in robot_items:
            trace = _item_base_trace(item, previous_xy)
            first_time, first_xy = trace[0]
            if first_time > cursor + 1e-12:
                owner = previous_item or item
                dwell_xy = previous_xy if previous_item is not None else first_xy
                segments.append({
                    "t0": cursor, "t1": first_time,
                    "p0": dwell_xy, "p1": dwell_xy,
                    "item": owner,
                    "occupancy": "initial_dwell" if previous_item is None
                    else "inter_step_dwell",
                    "motion": "dwelling",
                })
            for (t0, p0), (t1, p1) in zip(trace, trace[1:]):
                if t1 <= t0 + 1e-12:
                    continue
                segments.append({
                    "t0": t0, "t1": t1, "p0": p0, "p1": p1,
                    "item": item, "occupancy": "active",
                    "motion": _motion(item),
                })
            cursor = float(item["start"]) + float(item["duration"])
            previous_xy = np.asarray(trace[-1][1], dtype=float)
            previous_item = item
        if previous_item is not None and makespan > cursor + 1e-12:
            segments.append({
                "t0": cursor, "t1": makespan,
                "p0": previous_xy, "p1": previous_xy,
                "item": previous_item,
                "occupancy": (
                    "inter_step_dwell"
                    if robot in unfinished_robots else "final_dwell"
                ),
                "motion": "dwelling",
            })
        result[robot] = segments
    return result


def serialize_base_occupancy_timelines(items, unfinished_robots=None):
    """JSON-safe exact base occupancy for deterministic resolver tools.

    This is compiler-to-resolver geometry, not part of the LLM payload.  Each
    segment covers active motion or an idle dwell in absolute schedule time.
    """
    timelines = {}
    for robot, segments in _base_occupancy_segments(
            items, unfinished_robots=unfinished_robots).items():
        timelines[robot] = [
            {
                "t0": float(segment["t0"]),
                "t1": float(segment["t1"]),
                "p0": [float(value) for value in segment["p0"][:2]],
                "p1": [float(value) for value in segment["p1"][:2]],
                "step": segment["item"]["id"],
                "occupancy": segment["occupancy"],
                "motion": segment["motion"],
            }
            for segment in segments
        ]
    return timelines


def _segment_position(segment, time_value):
    span = segment["t1"] - segment["t0"]
    if span <= 1e-12:
        return segment["p1"]
    fraction = min(
        1.0, max(0.0, (time_value - segment["t0"]) / span))
    return segment["p0"] + fraction * (segment["p1"] - segment["p0"])


def _continuous_segment_distance(a, b, lo, hi):
    """Exact minimum for two piecewise-linear base segments over ``[lo, hi]``."""
    pa = _segment_position(a, lo)
    pb = _segment_position(b, lo)
    va = (a["p1"] - a["p0"]) / max(a["t1"] - a["t0"], 1e-12)
    vb = (b["p1"] - b["p0"]) / max(b["t1"] - b["t0"], 1e-12)
    relative = pa - pb
    velocity = va - vb
    velocity_sq = float(np.dot(velocity, velocity))
    offset = (
        min(hi - lo, max(0.0, -float(np.dot(relative, velocity)) / velocity_sq))
        if velocity_sq > 1e-18 else 0.0
    )
    time_value = lo + offset
    pa = _segment_position(a, time_value)
    pb = _segment_position(b, time_value)
    return (
        float(np.linalg.norm(pa - pb)),
        0.5 * (pa + pb),
        time_value,
    )


# Ops whose base is translating (can be rerouted); everything else (pick,
# place, wait, reset, articulation skills) dwells at a pinned standoff and
# can only be time-shifted, never rerouted. See design doc §3b.
MOVING_OPS = {"navigate", "go_to"}

# kind→class→allowed-tools pruning (design doc §3b/§3c). Computed here, not
# left for the LLM to infer, so an out-of-class tool choice is a payload-level
# impossibility rather than a prompt-following hope.
ALLOWED_TOOLS_BY_CLASS = {
    "both_dwelling": ["add_after"],
    "one_moving": ["replan_path", "add_after"],
    "both_moving": ["replan_path", "add_after"],
}


def _motion(item):
    op = (item.get("completed_step") or {}).get("op")
    return "moving" if op in MOVING_OPS else "dwelling"


def _conflict_class(motion_a, motion_b):
    n_moving = (motion_a == "moving") + (motion_b == "moving")
    return ["both_dwelling", "one_moving", "both_moving"][n_moving]


def _blocked_party(a, b, motion_a, motion_b):
    """The party whose action should be adjusted to clear this conflict.

    one_moving: only the mover can be rerouted/delayed, so it's always the
    mover regardless of arrival order. both_dwelling/both_moving: no tool
    asymmetry between the two, so default to the later starter (the one that
    hasn't "committed" yet in program order, cheapest to delay further)."""
    if motion_a == "moving" and motion_b != "moving":
        return a["id"]
    if motion_b == "moving" and motion_a != "moving":
        return b["id"]
    return a["id"] if a["start"] >= b["start"] else b["id"]


def _last_step_by_robot(items):
    last_order = {}
    for it in items:
        robot = it["robot"]
        if robot not in last_order or it["order"] > last_order[robot]:
            last_order[robot] = it["order"]
    return last_order


def _conflict(kind, a, b, window, detail, message, *, cls=None, blocked=None,
              is_last=None, party_states=None, allowed_tools=None, extra=None):
    record = {
        "kind": kind,
        "steps": [a["id"], b["id"]],
        "robots": [a["robot"], b["robot"]],
        "window": window,
        "detail": detail,
        "message": message,
    }
    if cls is not None:
        record["class"] = cls
        record["allowed_tools"] = (
            list(allowed_tools) if allowed_tools is not None
            else ALLOWED_TOOLS_BY_CLASS[cls]
        )
        if party_states is None:
            party_states = [
                {"motion": _motion(a), "occupancy": "active"},
                {"motion": _motion(b), "occupancy": "active"},
            ]
        record["parties"] = []
        for item, state in zip((a, b), party_states):
            party = {
                "step": item["id"],
                "robot": item["robot"],
                "motion": state["motion"],
                "is_last_step": is_last[item["id"]],
            }
            if state.get("occupancy") != "active":
                party["occupancy"] = state["occupancy"]
            record["parties"].append(party)
        record["blocked"] = blocked
    if extra:
        record.update(extra)
    return record


def _path_cluster_key(a_segment, b_segment):
    """Logical identity used to merge adjacent keyframe-level violations."""
    final = next(
        (segment for segment in (a_segment, b_segment)
         if segment["occupancy"] == "final_dwell"),
        None,
    )
    if final is not None:
        other = b_segment if final is a_segment else a_segment
        other_item = other["item"]
        return (
            "final_dwell",
            final["item"]["robot"], final["item"]["id"],
            other_item["robot"], other_item.get("group") or other_item["id"],
        )
    return (
        a_segment["item"]["robot"], a_segment["item"]["id"],
        a_segment["occupancy"],
        b_segment["item"]["robot"], b_segment["item"]["id"],
        b_segment["occupancy"],
    )


def _continuous_path_conflicts(
        items, last_order, focus_step_id=None, unfinished_robots=None):
    """Continuous proximity conflicts over complete robot occupancy timelines."""
    timelines = _base_occupancy_segments(
        items, unfinished_robots=unfinished_robots)
    robot_names = sorted(timelines)
    candidates = []
    for left_index, robot_a in enumerate(robot_names):
        for robot_b in robot_names[left_index + 1:]:
            segments_a, segments_b = timelines[robot_a], timelines[robot_b]
            index_a = index_b = 0
            while index_a < len(segments_a) and index_b < len(segments_b):
                segment_a, segment_b = segments_a[index_a], segments_b[index_b]
                lo = max(segment_a["t0"], segment_b["t0"])
                hi = min(segment_a["t1"], segment_b["t1"])
                focused = (
                    focus_step_id is None
                    or segment_a["item"]["id"] == focus_step_id
                    or segment_b["item"]["id"] == focus_step_id
                )
                if focused and lo < hi - 1e-12:
                    min_dist, at_xy, at_time = _continuous_segment_distance(
                        segment_a, segment_b, lo, hi)
                    if min_dist < STANDOFF_MIN_DIST:
                        candidates.append({
                            "key": _path_cluster_key(segment_a, segment_b),
                            "lo": lo, "hi": hi, "min_dist": min_dist,
                            "at_xy": at_xy, "at_time": at_time,
                            "a": segment_a, "b": segment_b,
                        })
                if segment_a["t1"] <= segment_b["t1"] + 1e-12:
                    index_a += 1
                if segment_b["t1"] <= segment_a["t1"] + 1e-12:
                    index_b += 1

    # A full trace creates many adjacent keyframe-level violations. Merge them
    # without merging across a safe temporal gap. final_dwell uses the other
    # party's semantic group so navigate/close/reset in one task becomes one
    # insert-go-to scenario instead of warning spam.
    merged = []
    by_key = {}
    for candidate in sorted(candidates, key=lambda c: (c["key"], c["lo"])):
        previous = by_key.get(candidate["key"])
        if previous is None or candidate["lo"] > previous["hi"] + 1e-9:
            previous = {
                **candidate,
                "representative": candidate,
                "best": candidate,
            }
            merged.append(previous)
            by_key[candidate["key"]] = previous
            continue
        previous["hi"] = max(previous["hi"], candidate["hi"])
        if candidate["min_dist"] < previous["best"]["min_dist"]:
            previous["best"] = candidate

    conflicts = []
    for interval in merged:
        representative = interval["representative"]
        best = interval["best"]
        segment_a, segment_b = representative["a"], representative["b"]
        a, b = segment_a["item"], segment_b["item"]
        motion_a, motion_b = segment_a["motion"], segment_b["motion"]
        cls = _conflict_class(motion_a, motion_b)
        blocked = _blocked_party(a, b, motion_a, motion_b)
        final_dwell = (
            segment_a["occupancy"] == "final_dwell"
            or segment_b["occupancy"] == "final_dwell"
        )
        allowed_tools = None
        extra = None
        if final_dwell:
            # add_after against the completed last step is a no-op. The repair
            # kernel must first create a real departure anchor.
            allowed_tools = (
                ["replan_path", "insert_go_to"]
                if cls == "one_moving" else ["insert_go_to"]
            )
            extra = {"requires_departure_anchor": True}
        window = [round(interval["lo"], 3), round(interval["hi"], 3)]
        at_xy = best["at_xy"]
        tag = f"{a['robot']}/{a['label']} & {b['robot']}/{b['label']}"
        suffix = " (includes final-pose occupancy)" if final_dwell else ""
        conflicts.append(_conflict(
            "path", a, b, window,
            {
                "min_dist": round(best["min_dist"], 3),
                "at_xy": [
                    round(float(at_xy[0]), 3),
                    round(float(at_xy[1]), 3),
                ],
                "at_time": round(float(best["at_time"]), 3),
            },
            f"bases {best['min_dist']:.2f}m apart "
            f"(< {STANDOFF_MIN_DIST}m): {tag} overlap in time{suffix}",
            cls=cls,
            blocked=blocked,
            is_last={
                a["id"]: a["order"] == last_order[a["robot"]],
                b["id"]: b["order"] == last_order[b["robot"]],
            },
            party_states=[
                {
                    "motion": motion_a,
                    "occupancy": segment_a["occupancy"],
                },
                {
                    "motion": motion_b,
                    "occupancy": segment_b["occupancy"],
                },
            ],
            allowed_tools=allowed_tools,
            extra=extra,
        ))
    return conflicts


def detect_conflicts(items, focus_step_id=None, unfinished_robots=None):
    """Structured conflicts (surfaced, not resolved — DG3). Each record has a
    `kind` (placement | facility | object | path), the two step ids/robots, the
    overlap `window` (None for the time-independent placement check), a `detail`
    payload, and a human `message`. `path` analytically checks every
    piecewise-linear base-track segment over the full schedule makespan,
    including idle gaps and final-pose occupancy.

    `facility` and `path` conflicts (the LLM-delegable kinds, design doc §3b/§3d)
    additionally carry `class` (both_dwelling | one_moving | both_moving),
    `allowed_tools` (pruned per class), `parties` (per-side motion +
    is_last_step), and `blocked` (which party's action should be adjusted).
    `placement`/`object` conflicts are intent/capacity issues escalated to a
    human and carry none of this — there's no tool to prune for them.

    compile_plan renders `warnings` as [c["message"] for c in conflicts], so the
    existing string surface (and its `robot/label` tokens the UI tints bars on)
    is preserved verbatim.

    ``focus_step_id`` returns the exact projection involving one current step.
    Compiler V2 uses it after a verified prefix: historical pairs cannot become
    newly conflicting until one of their steps changes, so recomputing them on
    every reservation check is redundant. ``unfinished_robots`` distinguishes
    temporary incremental-frontier occupancy from true terminal dwell.
    """
    last_order = _last_step_by_robot(items)
    # Facility sharing uses the exact same continuous base-clearance standard
    # as path collision detection.  Compute it once and retain the historical
    # facility record only for same-facility step pairs that are physically
    # closer than STANDOFF_MIN_DIST; a shared label alone is not contention.
    path_conflicts = _continuous_path_conflicts(
        items, last_order, focus_step_id=focus_step_id,
        unfinished_robots=unfinished_robots)
    path_proximity = {}
    for conflict in path_conflicts:
        key = frozenset(conflict["steps"])
        previous = path_proximity.get(key)
        if (previous is None
                or conflict["detail"]["min_dist"]
                < previous["detail"]["min_dist"]):
            path_proximity[key] = conflict
    conflicts = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if (focus_step_id is not None
                    and focus_step_id not in (a["id"], b["id"])):
                continue
            # placement collision — time-independent
            if a["place_xyz"] and b["place_xyz"]:
                d = float(np.linalg.norm(np.array(a["place_xyz"]) - np.array(b["place_xyz"])))
                if d < OBJ_MIN_DIST:
                    conflicts.append(_conflict(
                        "placement", a, b, None,
                        {"min_dist": round(d, 3), "at_xy": [round(float(v), 3) for v in a["place_xyz"][:2]],
                         "facility": a["facility"], "object": None},
                        f"placement collision: '{a['object']}' and '{b['object']}' dropped "
                        f"{d:.2f}m apart at the same point ({a['facility']})"))
            # contention — needs a time overlap between two different robots
            if a["robot"] == b["robot"] or not _overlap(a, b):
                continue
            lo = max(a["start"], b["start"])
            hi = min(a["start"] + a["duration"], b["start"] + b["duration"])
            window = [round(lo, 3), round(hi, 3)]
            tag = f"{a['robot']}/{a['label']} & {b['robot']}/{b['label']}"
            motion_a, motion_b = _motion(a), _motion(b)
            cls = _conflict_class(motion_a, motion_b)
            blocked = _blocked_party(a, b, motion_a, motion_b)
            proximity = path_proximity.get(frozenset((a["id"], b["id"])))
            if (a["facility"] and a["facility"] == b["facility"]
                    and proximity is not None):
                conflicts.append(_conflict(
                    "facility", a, b, window, {
                        "facility": a["facility"],
                        "min_dist": proximity["detail"]["min_dist"],
                        "distance_threshold": STANDOFF_MIN_DIST,
                    },
                    f"shared facility '{a['facility']}': {tag} overlap in time "
                    f"with bases {proximity['detail']['min_dist']:.2f}m apart "
                    f"(< {STANDOFF_MIN_DIST}m)",
                    cls=cls, blocked=blocked, is_last={
                        a["id"]: a["order"] == last_order[a["robot"]],
                        b["id"]: b["order"] == last_order[b["robot"]]}))
            if a["object"] and a["object"] == b["object"]:
                conflicts.append(_conflict(
                    "object", a, b, window, {"object": a["object"]},
                    f"shared object '{a['object']}': {tag} overlap in time"))
    conflicts.extend(path_conflicts)
    return conflicts


class IncrementalCompileConflict(ValueError):
    """Stage-3 V2 stop: the current navigation needs a repair not yet enabled."""

    def __init__(self, step_id, conflicts, committed_step_ids, attempts=None):
        self.step_id = step_id
        self.conflicts = copy.deepcopy(conflicts)
        self.committed_step_ids = list(committed_step_ids)
        self.attempts = copy.deepcopy(attempts or [])
        super().__init__(
            f"Compiler V2 navigation '{step_id}' conflicts with the committed "
            "reservation prefix")

    def as_dict(self):
        return {
            "code": "compiler_v2_repair_required",
            "step_id": self.step_id,
            "conflicts": copy.deepcopy(self.conflicts),
            "committed_step_ids": list(self.committed_step_ids),
            "attempts": copy.deepcopy(self.attempts),
        }


def _incremental_initial_anchors(items):
    """Represent every robot's t=0 pose before its first committed step."""
    first_by_robot = {}
    for item in items:
        current = first_by_robot.get(item["robot"])
        if current is None or item["order"] < current["order"]:
            first_by_robot[item["robot"]] = item
    anchors = []
    for robot, first in first_by_robot.items():
        raw_trace = first.get("base_trace") or []
        if raw_trace and len(raw_trace[0]) >= 3:
            xy = [float(raw_trace[0][1]), float(raw_trace[0][2])]
        else:
            xy = [float(value) for value in first["base_xy"][:2]]
        anchor_id = f"__compiler_v2_initial__{robot}"
        anchors.append({
            "id": anchor_id,
            "robot": robot,
            "start": 0.0,
            "order": -1,
            "duration": 0.0,
            "base_xy": xy,
            "base_trace": [[0.0, *xy], [0.0, *xy]],
            "after": [],
            "facility": None,
            "object": None,
            "place_xyz": None,
            "label": anchor_id,
            "group": anchor_id,
            "completed_step": {
                "id": anchor_id, "op": "reset", "group": anchor_id,
            },
        })
    return anchors


def _select_incremental_ready(programs, cursors, robot_end, completed_end):
    """Select the chronological ready cursor with a stable robot-id tie break."""
    ready = []
    blocked = {}
    for robot, steps in programs.items():
        cursor = cursors[robot]
        if cursor >= len(steps):
            continue
        step = steps[cursor]
        dependencies = list(step.get("after", []))
        missing = [dep for dep in dependencies if dep not in completed_end]
        if missing:
            blocked[step["id"]] = missing
            continue
        earliest = max(
            [robot_end[robot], *(completed_end[dep] for dep in dependencies)],
            default=robot_end[robot])
        ready.append((round(float(earliest), 3), str(robot), step))
    if ready:
        return min(ready, key=lambda value: (value[0], value[1]))

    dependencies = {
        step["id"]: set(step.get("after", []))
        for steps in programs.values() for step in steps
    }
    for steps in programs.values():
        for previous, current in zip(steps, steps[1:]):
            dependencies[current["id"]].add(previous["id"])
    cycle = _find_dependency_cycle(dependencies)
    if cycle is not None:
        raise ValueError(
            "dependency cycle in Compiler V2 ready frontier: "
            + " -> ".join(cycle))
    raise ValueError(f"Compiler V2 ready frontier is blocked: {blocked}")


def _incremental_candidate_conflicts(
        anchors, committed, candidates, step_id, unfinished_robots=None):
    return [
        conflict
        for conflict in detect_conflicts(
            [*anchors, *committed, *candidates], focus_step_id=step_id,
            unfinished_robots=unfinished_robots)
        if conflict.get("kind") in ("path", "facility")
        and step_id in conflict.get("steps", [])
    ]


def incremental_schedule_no_repair(items):
    """Chronologically schedule/reserve items and stop at the first conflict.

    Same-robot order comes from each robot cursor. Explicit ``after`` edges are
    the only additional readiness gate. Navigation queries reuse the current
    conflict detector against committed moving and stationary occupancy.
    """
    by_id = {}
    by_robot = {}
    for item in items:
        sid = item["id"]
        if sid in by_id:
            raise ValueError(f"duplicate step id '{sid}'")
        by_id[sid] = item
        by_robot.setdefault(item["robot"], []).append(item)
    for robot_items in by_robot.values():
        robot_items.sort(key=lambda value: value["order"])

    for item in items:
        unknown = sorted(
            dependency for dependency in item.get("after", [])
            if dependency not in by_id)
        if unknown:
            raise ValueError(
                f"step '{item['id']}' depends on unknown step id(s): {unknown}")

    cursors = {robot: 0 for robot in by_robot}
    robot_end = {robot: 0.0 for robot in by_robot}
    completed_end = {}
    committed = []
    anchors = _incremental_initial_anchors(items)
    commit_order = []
    reservation_checks = 0

    while len(committed) < len(items):
        start, robot, candidate = _select_incremental_ready(
            by_robot, cursors, robot_end, completed_end)
        candidate["start"] = start

        op = (candidate.get("completed_step") or {}).get("op")
        if op in MOVING_OPS:
            reservation_checks += 1
            unfinished_robots = {
                name for name, robot_items in by_robot.items()
                if cursors[name] + (1 if name == robot else 0)
                < len(robot_items)
            }
            relevant = _incremental_candidate_conflicts(
                anchors, committed, [candidate], candidate["id"],
                unfinished_robots=unfinished_robots)
            if relevant:
                raise IncrementalCompileConflict(
                    candidate["id"], relevant,
                    [item["id"] for item in committed])

        committed.append(candidate)
        commit_order.append(candidate["id"])
        end = round(start + float(candidate["duration"]), 3)
        completed_end[candidate["id"]] = end
        robot_end[robot] = end
        cursors[robot] += 1

    return {
        "items": items,
        "commit_order": commit_order,
        "completed_end": completed_end,
        "reservation_checks": reservation_checks,
    }


def _joint_qpos_span(model, joint_name):
    jid = mujoco.mj_name2id(
        model, mujoco.mjtObj.mjOBJ_JOINT, joint_name)
    if jid < 0:
        raise ValueError(f"shared-world joint not found: '{joint_name}'")
    adr = int(model.jnt_qposadr[jid])
    jtype = int(model.jnt_type[jid])
    if jtype == int(mujoco.mjtJoint.mjJNT_FREE):
        width = 7
    elif jtype == int(mujoco.mjtJoint.mjJNT_BALL):
        width = 4
    else:
        width = 1
    return adr, width


def _make_shared_world(rig, manifest, initial_q=None):
    fixture_spans = []
    seen = set()
    facility_state = {}
    for facility, spec in manifest["facilities"].items():
        art = spec.get("articulation") or {}
        if art:
            facility_state[facility] = art.get("initial_state")
        for joint_name in art.get("joints") or []:
            span = _joint_qpos_span(rig.model, joint_name)
            if span not in seen:
                fixture_spans.append(span)
                seen.add(span)
    object_spans = {
        obj: _joint_qpos_span(rig.model, _obj_joint(obj))
        for obj in manifest["objects"]
    }
    world_q = (rig.model.qpos0 if initial_q is None else initial_q).copy()
    # Objects that begin inside a declared container already rest on that
    # fixture before the first authored step. Register them exactly like
    # objects released by a runtime place; otherwise an initial CloseDrawer
    # moves only the slide joint and leaves its contents floating in world
    # coordinates. Picking one later removes it from this table in
    # compile_robot, while the remaining contents continue to follow.
    resting = {}
    for obj, obj_spec in manifest["objects"].items():
        facility = obj_spec.get("home_facility")
        facility_spec = manifest["facilities"].get(facility) or {}
        place = facility_spec.get("place") or {}
        articulation = facility_spec.get("articulation") or {}
        if (place.get("kind") != "container"
                or articulation.get("initial_state") != "open"):
            continue
        adr, width = object_spans[obj]
        if width != 7:
            continue
        resting.setdefault(facility, {})[obj] = (
            world_q[adr:adr + 3].copy(),
            world_q[adr + 3:adr + 7].copy(),
        )
    return {
        "q": world_q,
        "fixture_spans": fixture_spans,
        "object_spans": object_spans,
        "facility_state": facility_state,
        "held_by": {},
        # facility -> {object: (position, quaternion)}. All objects in one
        # facility are settled/replayed in a single MuJoCo rollout.
        "resting": resting,
        "initial_resting_facilities": set(resting),
    }


def _step_skill_name(step):
    op = step["op"]
    return step.get("name", op) if op not in CORE_OPS else None


class TopologyError(ValueError):
    """A plan dependency error safe to return from the lightweight validator."""

    def __init__(self, code, message, *, cycle=None, facility=None):
        super().__init__(message)
        self.code = code
        self.cycle = list(cycle) if cycle is not None else None
        self.facility = facility

    def as_dict(self):
        return {
            "code": self.code,
            "message": str(self),
            "cycle": self.cycle,
            "facility": self.facility,
        }


def _dependency_reaches(dependencies, start, target):
    """Whether ``start`` waits (directly or transitively) for ``target``."""
    pending = [start]
    seen = set()
    while pending:
        node = pending.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        pending.extend(dependencies.get(node, ()))
    return False


def _placement_compile_order(cycles, dependencies, facility):
    """Choose a deterministic serial shared-world order for one container.

    A placement cycle is compiled atomically from its approach/pick start through
    its placement/reset completion.  ``compile_only`` serialization may choose
    either order only when the authored dependency graph permits both.  If B's
    completion already waits for A's start, B cannot be compiled before A: the
    inverse serialization would add ``A.start waits for B.completion`` and
    immediately close a dependency loop.  Author order is therefore only the
    stable tie-break among unconstrained cycles.
    """
    unit_deps = {cycle["cycle_start"]["id"]: set() for cycle in cycles}
    by_start = {cycle["cycle_start"]["id"]: cycle for cycle in cycles}
    for earlier in cycles:
        for later in cycles:
            if earlier is later:
                continue
            # ``later`` must be after ``earlier`` when its completion waits
            # for the earlier cycle to have begun.  The unit graph preserves
            # that relation while the final serialization strengthens it to
            # completion-before-start.
            if _dependency_reaches(
                    dependencies,
                    later["completion"]["id"],
                    earlier["cycle_start"]["id"]):
                unit_deps[later["cycle_start"]["id"]].add(
                    earlier["cycle_start"]["id"])

    cycle = _find_dependency_cycle(unit_deps)
    if cycle is not None:
        raise TopologyError(
            "container_serialization_cycle",
            f"container placement serialization cycle for '{facility}' "
            "(each placement must follow the next): " + " -> ".join(cycle),
            cycle=cycle,
            facility=facility,
        )

    outgoing = {sid: [] for sid in unit_deps}
    indegree = {sid: len(required) for sid, required in unit_deps.items()}
    for sid, required in unit_deps.items():
        for dep in required:
            outgoing[dep].append(sid)
    ready = sorted(
        (by_start[sid] for sid, degree in indegree.items() if degree == 0),
        key=lambda cycle: (cycle["cycle_start"]["_author_order"],
                           cycle["cycle_start"]["id"]),
    )
    ordered = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        sid = current["cycle_start"]["id"]
        for target in outgoing[sid]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(by_start[target])
                ready.sort(key=lambda cycle: (
                    cycle["cycle_start"]["_author_order"],
                    cycle["cycle_start"]["id"],
                ))
    # The cycle case above makes this defensive condition unreachable, but keep
    # the failure local if this routine is changed later.
    if len(ordered) != len(cycles):
        raise TopologyError(
            "container_serialization_incomplete",
            f"container placement serialization did not order all cycles for "
            f"'{facility}'",
            facility=facility,
        )
    return ordered


def validate_plan_topology(plans, manifest):
    """Validate all non-geometric scheduling constraints without MuJoCo work.

    This deliberately calls the exact container dependency builder used by
    :func:`compile_plan`, on a private flattened copy.  It is the shared
    preflight entry point for manual edits and resolver candidates; a success
    says only that the candidate is topologically compilable, not that motion
    planning or collision checking will succeed.
    """
    if not isinstance(manifest, dict):
        manifest = json.loads(Path(manifest).read_text("utf-8"))
    flat = flatten_tasks(json.loads(json.dumps(plans)))
    _container_dependency_order(flat, manifest)
    return flat


def _container_dependency_order(plans, manifest):
    """Return a stable topological step order and materialise implicit `after`.

    Per-robot program order remains authoritative. Container state adds the
    missing cross-robot edges: Open precedes each object's navigation to the
    facility (but not its source navigation/pick), placements in one facility
    are serialized, and Close follows every completed placement.
    """
    nodes = []
    by_id = {}
    by_robot = {}
    for robot, steps in plans.items():
        by_robot[robot] = steps
        for step in steps:
            sid = step["id"]
            if sid in by_id:
                raise ValueError(f"duplicate step id '{sid}'")
            node = (robot, step)
            nodes.append(node)
            by_id[sid] = node

    deps = {step["id"]: set(step.get("after", [])) for _, step in nodes}
    # Geometry compilation has a deterministic shared-world order, but that
    # internal order must not silently become execution policy. In particular,
    # same-container placements compile in author order so later tracks see
    # earlier resting objects, while their runtime schedules remain parallel
    # until detect_conflicts + the repair scheduler decide how to coordinate.
    compile_only_deps = {step["id"]: set() for _, step in nodes}
    for sid, required in deps.items():
        missing = sorted(dep for dep in required if dep not in by_id)
        if missing:
            raise ValueError(
                f"step '{sid}' depends on unknown step id(s): {missing}")

    # A robot's qpos/held-object context is sequential even when no explicit
    # dependency was authored.
    for steps in by_robot.values():
        for previous, current in zip(steps, steps[1:]):
            deps[current["id"]].add(previous["id"])

    for facility, spec in manifest["facilities"].items():
        place_spec = spec.get("place") or {}
        if place_spec.get("kind") != "container":
            continue
        places = sorted(
            [(robot, step) for robot, step in nodes
             if step["op"] == "place" and step.get("dest") == facility],
            key=lambda node: node[1]["_author_order"])
        if not places:
            continue

        required_open = place_spec.get("requires_open")
        open_nodes = [
            node for node in nodes
            if _step_skill_name(node[1]) == required_open
        ] if required_open else []
        initial_state = (
            (spec.get("articulation") or {}).get("initial_state")
            or (manifest.get("state_model", {}).get(facility, {}) or {}).get(
                "initial")
            or "closed"
        )
        expected_open_count = 0 if initial_state == "open" else 1
        if required_open and len(open_nodes) != expected_open_count:
            raise ValueError(
                f"container '{facility}' starts {initial_state!r} and requires "
                f"{expected_open_count} '{required_open}' step(s) before "
                f"placement; found {len(open_nodes)}")
        open_node = open_nodes[0] if open_nodes else None

        close_name = (
            (spec.get("articulation") or {}).get("skills") or {}
        ).get("close")
        close_nodes = [
            node for node in nodes
            if close_name and _step_skill_name(node[1]) == close_name
        ]
        if len(close_nodes) > 1:
            raise ValueError(
                f"container '{facility}' has multiple '{close_name}' steps; "
                "multiple open/close sessions need explicit session IDs")

        completions = []
        placement_cycles = []
        for robot, place in places:
            robot_steps = by_robot[robot]
            place_index = robot_steps.index(place)
            pick_index = next(
                (idx for idx in range(place_index - 1, -1, -1)
                 if robot_steps[idx]["op"] == "pick"
                 and robot_steps[idx].get("object") == place.get("object")),
                None)
            cycle_start = place
            facility_entry = None
            if pick_index is not None:
                cycle_start = robot_steps[pick_index]
                if (pick_index > 0
                        and robot_steps[pick_index - 1]["op"] == "navigate"
                        and robot_steps[pick_index - 1].get("target")
                        == place.get("object")):
                    cycle_start = robot_steps[pick_index - 1]
                facility_entry = next(
                    (robot_steps[idx]
                     for idx in range(pick_index + 1, place_index)
                     if robot_steps[idx]["op"] == "navigate"
                     and robot_steps[idx].get("target") == facility),
                    None)

            if open_node is not None:
                # Retrieving the object can overlap another robot opening the
                # container. Gate only the second navigation, from the picked
                # object to the destination facility, so a robot may navigate
                # to and pick its object while Open is still running.
                #
                # Preserve legacy plans that deliberately reach the facility
                # before opening it while holding: gating an earlier facility
                # navigation on a later same-robot Open would create a cycle.
                if (facility_entry is not None
                        and open_node[1]["_author_order"]
                        < facility_entry["_author_order"]):
                    deps[facility_entry["id"]].add(open_node[1]["id"])
                # Keep the physical operation itself guarded even for compact
                # or legacy plans that omit a recognizable facility navigate.
                deps[place["id"]].add(open_node[1]["id"])
            completion = place
            if (place_index + 1 < len(robot_steps)
                    and robot_steps[place_index + 1]["op"] == "reset"):
                completion = robot_steps[place_index + 1]
            completions.append(completion)
            placement_cycles.append({
                "cycle_start": cycle_start,
                "completion": completion,
            })

        if close_nodes:
            close_robot, close_step = close_nodes[0]
            close_start = close_step
            close_robot_steps = by_robot[close_robot]
            close_index = close_robot_steps.index(close_step)
            close_group = close_step.get("group")
            if close_group is not None:
                # A semantic close task expands to reset -> navigate -> Close ->
                # reset. Gate the whole task behind all placements; otherwise
                # its first reset starts early and task-view spans a long idle
                # gap even though the actual close is correctly delayed.
                close_start = next(
                    step for step in close_robot_steps
                    if step.get("group") == close_group
                )
            elif (close_index > 0
                    and close_robot_steps[close_index - 1]["op"] == "navigate"
                    and close_robot_steps[close_index - 1].get("target")
                    == facility):
                close_start = close_robot_steps[close_index - 1]
            for completion in completions:
                deps[close_start["id"]].add(completion["id"])
                deps[close_step["id"]].add(completion["id"])

        # The geometry compiler must replay resting objects in one serial
        # order.  That is an implementation detail, but it must agree with
        # the candidate's authored/inferred execution dependencies.  Select
        # the order *after* open/close edges are present so all constraints are
        # considered; author order is only used for unconstrained ties.
        ordered_cycles = _placement_compile_order(
            placement_cycles, deps, facility)
        for previous, current in zip(ordered_cycles, ordered_cycles[1:]):
            previous_completion = previous["completion"]["id"]
            current_start = current["cycle_start"]["id"]
            # Adding current -> previous is safe iff previous does not already
            # wait for current in the graph built so far.  Check incrementally
            # as a final proof, rather than relying only on unit ordering.
            existing = {
                sid: deps[sid] | compile_only_deps[sid]
                for sid in deps
            }
            if _dependency_reaches(existing, previous_completion, current_start):
                raise TopologyError(
                    "container_serialization_cycle",
                    f"container placement serialization cycle for '{facility}': "
                    f"{current_start} cannot wait for {previous_completion}",
                    cycle=[previous_completion, current_start, previous_completion],
                    facility=facility,
                )
            compile_only_deps[current_start].add(previous_completion)

    # Only cross-robot / explicitly inferred edges need to be written back for
    # schedule(); per-robot ordering is already enforced by robot availability.
    for robot, step in nodes:
        authored = list(step.get("after", []))
        for dep in sorted(deps[step["id"]]):
            dep_robot = by_id[dep][0]
            if dep_robot != robot and dep not in authored:
                authored.append(dep)
        step["after"] = authored

    topo_deps = {
        sid: deps[sid] | compile_only_deps[sid]
        for sid in by_id
    }
    outgoing = {sid: [] for sid in by_id}
    indegree = {sid: len(required) for sid, required in topo_deps.items()}
    for sid, required in topo_deps.items():
        for dep in required:
            outgoing[dep].append(sid)
    ready = sorted(
        (by_id[sid] for sid, degree in indegree.items() if degree == 0),
        key=lambda node: node[1]["_author_order"])
    ordered = []
    while ready:
        node = ready.pop(0)
        ordered.append(node)
        sid = node[1]["id"]
        for target in outgoing[sid]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(by_id[target])
                ready.sort(key=lambda item: item[1]["_author_order"])
    if len(ordered) != len(nodes):
        blocked = sorted(sid for sid, degree in indegree.items() if degree)
        cycle = _find_dependency_cycle(topo_deps)
        cycle_detail = (
            " -> ".join(cycle) if cycle is not None else "not found"
        )
        raise TopologyError(
            "dependency_cycle",
            "dependency cycle in authored/shared-world steps "
            f"(each step waits for the next): {cycle_detail}; "
            f"blocked={blocked}",
            cycle=cycle,
        )
    return ordered


def _track_robot_indices(track):
    """Return robot indices named by a track's metadata and control channels."""
    indices = set()
    meta_index = (track.get("meta") or {}).get("robot_index")
    if isinstance(meta_index, int):
        indices.add(meta_index)
    for channel in (track.get("channels") or {}):
        for prefix in ("mobilebase", "robot", "gripper"):
            if not channel.startswith(prefix):
                continue
            suffix = channel[len(prefix):]
            digits = suffix[:len(suffix) - len(suffix.lstrip("0123456789"))]
            if digits and suffix[len(digits):].startswith("_"):
                indices.add(int(digits))
            break
    return indices


def _validate_track_robot(item):
    """Fail compile if a schedule lane disagrees with the track it will drive."""
    robot_name = item["robot"]
    expected = int(robot_name.removeprefix("robot"))
    actual = _track_robot_indices(item["track"])
    if actual != {expected}:
        found = ", ".join(f"robot{index}" for index in sorted(actual)) or "none"
        raise ValueError(
            f"track robot mismatch for step '{item['id']}' "
            f"({item['label']}): scheduled on {robot_name}, "
            f"but track controls {found}"
        )


# ------------------------------------------------------------- D7: step memo
#
# compile_plan's per-step loop below calls compile_robot(rig, robot_name,
# [step], ..., context=contexts[robot_name], shared_world=shared_world) one
# step at a time, in COMPILE order (ordered_steps from
# _container_dependency_order, not authoring order). That call's only state
# carriers are explicit — contexts[robot_name] and shared_world — and its only
# other inputs are the step itself, the step that follows it on the same
# robot (navigate peeks ahead to preview an upcoming skill's entry pose), and
# everything compiler_generation()-equivalent covers (scene, standoffs,
# manifest, base tracks, this file). So a step's output is a pure function of
# (generation, state reaching it, step, next_step).
#
# D7 shipped keying on a running hash of the COMPILE-ORDER PREFIX (every
# (robot, step, next_step) seen so far, in compile order), reasoning that the
# prefix determines the state reaching a step. True, but coarser than it
# needs to be: two runs whose compile orders interleave two robots' steps
# differently produce different prefix hashes even when neither robot's own
# reaching state actually differs — one robot's edit then spuriously misses
# the OTHER robot's unrelated steps too. D7b (docs/compound_turn_integration
# _spec.md §10, "D7b") fixes this — cause B there — by hashing the state
# itself instead of the path taken to reach it:
#
#   key = H(generation, digest(context_before), digest(shared_world_before),
#           canonical(step), canonical(next_step))
#
# digest() (_memo_state_digest below) is an exact byte-for-byte hash of the
# live context/shared_world structures — numpy arrays included, at full
# precision, never rounded, because float drift changing the compiled
# trajectory is exactly the case this cache must not paper over. This key is
# invariant to interleaving that does not change what either robot's
# compile_robot call actually sees, and still misses whenever it genuinely
# does. It also removes the need for D7's "memo_broken" poison-the-rest-of-
# the-compile latch: because the key is a function of real state rather than
# an accumulated hash chain, one step that can't be canonicalised only makes
# THAT step uncacheable — later steps' states are still exactly known (they
# came from a real compile_robot call, canonicalisable or not) and can still
# be keyed and cached normally.
#
# D7b also fixes cause A: a step compiled once under its AUTHORED shape needs
# to be found again when the resolver resubmits the same logical step in
# "completed" shape (extra compiler-filled fields — _author_order,
# _robot_order, robot, standoff; see _memo_canonical_step's docstring for why
# `after` is separately excluded from both shapes). Rather than deciding by
# hand which of those fields is geometry-relevant — `standoff` is NOT
# decidable that way, since a user can genuinely edit it and a wrong hit
# there is exactly the unsafe case this cache exists to avoid — every entry
# is ALSO indexed under the key its own `completed_step` would produce (see
# the pending-alias bookkeeping in compile_plan). Both keys resolve to one
# shared stored value and one LRU slot: _STEP_MEMO holds the real entries,
# _STEP_MEMO_ALIASES maps an alias key to the primary key backing it, and
# eviction removes both together (see _step_memo_add_alias /
# _evict_step_memo_over_cap_locked).
#
# A hit must fast-forward state, not just splice in cached items: compile_robot
# mutates contexts[robot_name] and shared_world in place, so anything after a
# skipped step needs the post-step versions of both, not the pre-step ones it
# was handed.

_STEP_MEMO_LOCK = threading.Lock()
# primary_key -> {"items": [...], "context": {...}, "shared_world": {...},
# "aliases": {alias_key, ...}}. items/context/shared_world are each a
# self-contained deep copy (see _step_memo_get/_step_memo_put) — load-bearing,
# not defensive slack: a hit installs the cached context/shared_world as LIVE
# state and the very next compile_robot call mutates them in place, so handing
# back the stored objects directly would let one compile corrupt every other
# entry keyed off the same state. This is also why `verify` mode (see
# _compile_memo_mode) is not total coverage of that risk: its hit branch never
# actually installs a cached entry as live state (gated on mode == "on"), so
# it only proves "the stored value was right when stored", not "using it
# doesn't corrupt the store" — verify passing is not evidence the deep copies
# could be dropped.
#
# Module-level and process-wide: skill_service keeps one warm process for the
# life of the server and serializes all compiles behind one lock
# (_MUJOCO_EXECUTION_LOCK), so this needs no coordination beyond its own lock,
# which only guards the dicts themselves against concurrent callers of
# compile_plan from outside that lock (tests, the __main__ self-test, etc).
_STEP_MEMO: "OrderedDict[str, dict]" = OrderedDict()
# alias_key -> primary_key currently backing it. An alias key is never itself
# a key into _STEP_MEMO's stored entries; every lookup resolves through this
# mapping first (see _step_memo_get).
_STEP_MEMO_ALIASES: "dict[str, str]" = {}


def _compile_memo_mode():
    """off: no memo, today's behaviour. on: use it. verify: recompute AND
    compare on every would-be hit, raising loudly on any mismatch, so the
    cache can be proven sound against the full test suite before anything
    relies on its speed.

    Default is "on": every hit is provably equivalent to a fresh compile by
    construction (deterministic geometry code, see RRT_SEED etc. — same
    inputs always produce the same outputs), the key constructions below MISS
    rather than guess whenever a field can't be canonicalised with
    confidence, and this ships alongside `verify` specifically so the
    equivalence can be checked mechanically rather than assumed. Set
    MJSKILL_COMPILE_MEMO=off to reproduce pre-D7 behaviour exactly, or
    =verify to audit it.
    """
    mode = os.environ.get("MJSKILL_COMPILE_MEMO", "on").strip().lower()
    if mode == "verify":
        return "verify"
    if mode in ("off", "0", "false", "no", "none"):
        return "off"
    return "on"


def _compile_memo_max_entries():
    # Trajectories are not small (a track carries every scripted joint's full
    # keyframe history), so this bounds memory, not just lookup cost.
    try:
        return max(1, int(os.environ.get("MJSKILL_COMPILE_MEMO_MAX_ENTRIES", "512")))
    except ValueError:
        return 512


def _path_fingerprint(path):
    """(resolved path, mtime_ns, size), or a None-triple if the file is missing.

    Same shape as skill_service.compiler_generation()'s per-file material —
    kept independent (rather than imported) so this module's own __main__
    self-test can exercise the memo without depending on the service layer.
    """
    p = Path(path)
    try:
        stat = p.stat()
        return (str(p.resolve()), stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        return (str(p.resolve()), None, None)


def tracks_fingerprint(tracks_dir):
    """Sorted (relative path, mtime_ns, size) for every file under
    ``tracks_dir``, EXCLUDING ``GENERATED_TRACK_DIR``.

    Base track files feed compiled output (Open/Close replays, the navigate
    lookahead that previews a following skill's entry pose) but were not
    previously part of any cache's identity — bumping one must invalidate the
    step memo and compiler_generation() exactly like editing this module
    would. ``_generated/`` is excluded because it is OUTPUT: it's the
    plan-hash-namespaced directory write_generated_tracks() writes into after
    every compile (see its docstring). Folding it into the identity would
    make every compile invalidate the very cache it's about to populate.
    Skipping descent into it (not just filtering afterward) also matters in
    practice — it accumulates a namespace directory per distinct plan ever
    compiled, so os.walk-ing it every request would dominate this fingerprint
    instead of costing nothing.
    """
    root = Path(tracks_dir)
    entries = []
    if not root.exists():
        return entries
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if child.name == GENERATED_TRACK_DIR:
            continue
        if child.is_file():
            stat = child.stat()
            entries.append((child.name, stat.st_mtime_ns, stat.st_size))
        elif child.is_dir():
            for file in sorted(child.rglob("*")):
                if file.is_file():
                    rel = file.relative_to(root)
                    stat = file.stat()
                    entries.append(
                        (str(rel).replace("\\", "/"), stat.st_mtime_ns, stat.st_size))
    return entries


def compile_memo_generation(scene_xml, standoffs_path, tracks_dir, manifest_path):
    """Identity of every input a compiled step can depend on, for the D7 step
    memo. Computed from the same paths compile_plan already receives, so a
    stale-generation bump here fires under exactly the same conditions as
    skill_service.compiler_generation() (which also folds in TRACKS via
    ``tracks_fingerprint`` — the two are deliberately kept in the same shape
    without one importing the other's module)."""
    navgrid_paths = navigation_grid_paths(scene_xml)
    material = [
        _path_fingerprint(scene_xml),
        _path_fingerprint(standoffs_path),
        _path_fingerprint(manifest_path),
        _path_fingerprint(navgrid_paths["npz"]),
        _path_fingerprint(navgrid_paths["json"]),
        _path_fingerprint(__file__),
        tracks_fingerprint(tracks_dir),
    ]
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _memo_canonical_step(step):
    """JSON-canonical form of a step, for hashing into the compile-order
    prefix — restricted to the fields that can actually change compile_robot's
    output.

    Deliberately EXCLUDES "after". Two reasons, both load-bearing:

    1. It isn't an input to the geometry at all. compile_robot never reads
       step["after"]; the only consumer is schedule(), which runs once, after
       every item already exists, using each item's own `after` (set below
       from the step actually being compiled, not from whatever produced a
       cached entry).
    2. _container_dependency_order rewrites it in place (skill_generators.py,
       search "authored = list(step.get" above) to add every inferred
       cross-robot dependency — container open/close ordering, same-container
       serialization — so by the time this runs it is not even a stable
       function of what was authored.

    Concretely, this is what makes the design doc's headline case work: an
    add_after edge that leaves the compile order unchanged must still hit on
    the very step that gained the edge. Hashing "after" would make that
    impossible by construction. The step's position in the compile-order
    prefix already captures everything an `after` edge can actually change —
    it can only relinearize ordered_steps (see _container_dependency_order) —
    so a miss still happens exactly where that reordering first shows up,
    with no special case needed to get it right.

    Raises TypeError/ValueError (caller treats both as "cannot canonicalise,
    so miss") for anything json can't round-trip deterministically: an
    unknown/opaque field, a numpy array smuggled onto a step, NaN/Infinity.
    Never guess a key.
    """
    if not isinstance(step, dict):
        raise TypeError("memo: step must be a dict")
    filtered = {k: v for k, v in step.items() if k != "after"}
    return json.dumps(
        filtered, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False)


def _memo_digest_bytes(value):
    """Recursively serialise a live context/shared_world value into bytes for
    hashing — the "digest(context_before)" / "digest(shared_world_before)"
    half of a D7b key (see the D7 step-memo comment block above _STEP_MEMO).

    Unlike _memo_canonical_step (which hashes an authored/completed STEP via
    json.dumps), context and shared_world are live Python/numpy structures,
    never JSON: qpos arrays in particular must be compared byte-for-byte —
    dtype, shape, and raw bytes, never rounded — because float drift changing
    the compiled trajectory downstream is exactly the case this cache must
    not paper over. Every branch is tagged with a type byte so values of
    different types can never collide onto the same bytes (e.g. the int 1
    vs. the string "1"). Raises TypeError for anything outside this closed
    set — every caller treats that as "cannot digest confidently, so miss
    this one step", never a guess (same discipline as _memo_canonical_step).
    """
    if value is None:
        return b"N"
    if isinstance(value, bool):
        return b"B1" if value else b"B0"
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return b"S" + len(encoded).to_bytes(8, "big") + encoded
    if isinstance(value, int):
        return b"I" + str(value).encode("ascii")
    if isinstance(value, float):
        # Exact IEEE-754 bits (not repr/str), so two floats differing only in
        # their last bit — real drift — are never hashed as equal.
        return b"F" + struct.pack(">d", value)
    if isinstance(value, np.ndarray):
        return (
            b"A" + str(value.dtype).encode("ascii") + b"|"
            + str(value.shape).encode("ascii") + b"|" + value.tobytes())
    if isinstance(value, dict):
        parts = [b"D", len(value).to_bytes(8, "big")]
        for k in sorted(value.keys()):
            if not isinstance(k, str):
                raise TypeError(
                    f"memo: cannot digest non-string dict key {k!r}")
            parts.append(_memo_digest_bytes(k))
            parts.append(_memo_digest_bytes(value[k]))
        return b"".join(parts)
    if isinstance(value, tuple):
        parts = [b"T", len(value).to_bytes(8, "big")]
        parts.extend(_memo_digest_bytes(v) for v in value)
        return b"".join(parts)
    if isinstance(value, list):
        parts = [b"L", len(value).to_bytes(8, "big")]
        parts.extend(_memo_digest_bytes(v) for v in value)
        return b"".join(parts)
    raise TypeError(
        f"memo: cannot digest value of type {type(value).__name__}")


def _memo_state_digest(value):
    """SHA-256 hex digest of `value` (a context or shared_world dict) via
    _memo_digest_bytes. Raises TypeError/ValueError exactly when
    _memo_digest_bytes does — caller treats that as "this step is
    uncacheable", never a guess."""
    return hashlib.sha256(_memo_digest_bytes(value)).hexdigest()


def _step_memo_key_material(
    generation, context, shared_world, step, next_step,
):
    """Build Compiler V2's state-sensitive key using V1's proven scheme.

    The caller treats any canonicalisation failure as an uncacheable step.
    ``after`` remains excluded by ``_memo_canonical_step`` because scheduling
    and reservation checks always run again after a geometry hit.
    """
    context_digest = _memo_state_digest(context)
    world_digest = _memo_state_digest(shared_world)
    step_canon = _memo_canonical_step_v2(step)
    next_canon = (
        _memo_canonical_step_v2(next_step)
        if next_step is not None else "null")
    key = hashlib.sha256(
        f"{generation}|{context_digest}|{world_digest}|{step_canon}|"
        f"{next_canon}".encode("utf-8")
    ).hexdigest()
    return {
        "key": key,
        "context_digest": context_digest,
        "world_digest": world_digest,
        "step_canon": step_canon,
        "next_canon": next_canon,
    }


def _step_memo_alias_key(
    generation, context_digest, world_digest, completed_canon,
    next_completed_canon,
):
    return hashlib.sha256(
        f"{generation}|{context_digest}|{world_digest}|{completed_canon}|"
        f"{next_completed_canon}".encode("utf-8")
    ).hexdigest()


def _memo_items_for_verify(raw_items, step):
    """Normalize fields that legitimately differ before cache verification."""
    out = copy.deepcopy(raw_items)
    _patch_v2_memo_hit_metadata(out, step)
    for item in out:
        item.pop("compile_time_sec", None)
    return out


def _memo_canonical_step_v2(step):
    """V2 geometry identity, excluding scheduling-only author order."""
    if not isinstance(step, dict):
        raise TypeError("memo: step must be a dict")
    filtered = {
        key: value for key, value in step.items()
        if key not in ("after", "_author_order")
    }
    return json.dumps(
        filtered, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False)


def _patch_v2_memo_hit_metadata(compiled_items, step):
    """Refresh non-geometric fields excluded from Compiler V2's memo key."""
    _patch_memo_hit_after(compiled_items, step)
    for item in compiled_items:
        completed_step = item.get("completed_step")
        if not isinstance(completed_step, dict):
            continue
        if "_author_order" in step:
            completed_step["_author_order"] = step["_author_order"]
        else:
            completed_step.pop("_author_order", None)


# D7b cause A (see the D7 step-memo comment block above _STEP_MEMO) aliases
# a cache entry under the key its own completed_step would produce, so a
# cached HIT can legitimately serve geometry that was originally computed
# from the AUTHORED step for a request that instead supplied that step's
# completed-form echo (completed_step["at"] / ["standoff"], rounded to 6
# decimals — see the "place_xyz = [round(...)" comments in compile_robot).
# That echo is not bit-exact versus the internal, full-precision value it
# echoes, and re-feeding it as an explicit override can nudge the IK's
# redundant wrist DOF into a measurably-if-microscopically different (still
# valid, still within the IK's own tol=4e-3) solution — single-digit
# microradians in practice, verified empirically against this scene. Exact
# `==`/`np.array_equal` would make `verify` mode raise on that legitimate,
# harmless noise for any plan containing a `place` step, which would make
# verify unusable for exactly the traffic D7b targets. The tolerance below
# is set to comfortably clear that noise floor while still catching a
# materially different trajectory (the actual failure mode this cache must
# never produce) — it does NOT weaken the cache KEY itself (digest/
# canonicalisation upstream stay byte-exact); it only widens the bar for
# what counts as "the same answer" once the key has already decided two
# inputs are equivalent.
_MEMO_VERIFY_FLOAT_RTOL = 1e-5
_MEMO_VERIFY_FLOAT_ATOL = 1e-5


def _memo_values_equal(a, b):
    """Structural equality that treats numpy arrays as data, not as objects
    whose truthiness needs a bool() (`==` on plain dicts/lists containing raw
    ndarrays raises "truth value of an array is ambiguous" — this is what
    verify mode above uses instead of `==` to compare cached vs freshly
    recomputed items/context/shared_world). Floats and arrays compare with a
    small tolerance (see _MEMO_VERIFY_FLOAT_RTOL/_ATOL above); everything
    else (strings, ints, bools, dict keys, list lengths) still compares
    exactly."""
    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        return (isinstance(a, np.ndarray) and isinstance(b, np.ndarray)
                and a.shape == b.shape
                and bool(np.allclose(
                    a, b, rtol=_MEMO_VERIFY_FLOAT_RTOL,
                    atol=_MEMO_VERIFY_FLOAT_ATOL)))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(
            _memo_values_equal(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(
            _memo_values_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(
            a, b, rel_tol=_MEMO_VERIFY_FLOAT_RTOL,
            abs_tol=_MEMO_VERIFY_FLOAT_ATOL)
    return a == b


def _patch_memo_hit_after(compiled_items, step):
    """Overwrite every cached item's `after` (both the top-level schedule
    field and, if present, the embedded `completed_step["after"]`) with the
    CURRENT step's real value.

    A memo hit's items were produced by whatever step first populated that
    prefix slot, which — by construction of _memo_canonical_step — may have
    had a different (or absent) `after` list than the step actually being
    compiled right now. `completed_step` is the artifact callers (resolver,
    UI) re-submit and edit, so it needs the same correction the scheduling
    field gets, or a re-submitted "completed" plan would silently regress a
    dependency edge a previous round added.
    """
    after = list(step.get("after", []))
    for item in compiled_items:
        item["after"] = list(after)
        completed_step = item.get("completed_step")
        if isinstance(completed_step, dict) and "after" in completed_step:
            completed_step["after"] = list(after)


def _evict_step_memo_over_cap_locked():
    """Drop oldest primary entries (and every alias pointing at them) until
    the cache is back at/under its cap. Caller must hold _STEP_MEMO_LOCK.
    Aliases are not separately capped/counted — an aliased entry is one LRU
    slot, not two — so this only ever pops from _STEP_MEMO, never directly
    from _STEP_MEMO_ALIASES."""
    max_entries = _compile_memo_max_entries()
    while len(_STEP_MEMO) > max_entries:
        primary_key, entry = _STEP_MEMO.popitem(last=False)
        for alias_key in entry.get("aliases", ()):
            if _STEP_MEMO_ALIASES.get(alias_key) == primary_key:
                del _STEP_MEMO_ALIASES[alias_key]


def _step_memo_get(key):
    """Resolve `key` (primary or alias) and return an independent deep copy
    of the cached entry plus the primary key backing it, or None.

    The copy matters as much as the lookup: schedule()/detect_conflicts
    mutate item dicts in place (start times etc.) and a restored context/
    shared_world are about to be mutated in place by the NEXT compile_robot
    call. Handing back the stored objects directly would let one compile
    corrupt every other cache entry keyed off the same state — see the
    "load-bearing, not defensive slack" comment above _STEP_MEMO.

    The returned `primary_key` lets a caller register a further alias
    against this same entry (D7b cause A: the completed-step index) without
    caring whether `key` itself was already a primary or an alias.
    """
    with _STEP_MEMO_LOCK:
        primary_key = _STEP_MEMO_ALIASES.get(key, key)
        entry = _STEP_MEMO.get(primary_key)
        if entry is None:
            return None
        _STEP_MEMO.move_to_end(primary_key)
        return {
            "items": copy.deepcopy(entry["items"]),
            "context": copy.deepcopy(entry["context"]),
            "shared_world": copy.deepcopy(entry["shared_world"]),
            "primary_key": primary_key,
        }


def _step_memo_put(key, items, context, shared_world):
    """Store a fresh entry under `key` as its PRIMARY key and return `key`.

    If `key` was previously registered as someone else's alias, it is
    promoted here to a primary in its own right (aliases only ever point at
    *current* primaries, so this can't leave `key` double-booked).
    """
    with _STEP_MEMO_LOCK:
        _STEP_MEMO[key] = copy.deepcopy({
            "items": items, "context": context, "shared_world": shared_world,
            "aliases": set(),
        })
        _STEP_MEMO.move_to_end(key)
        _STEP_MEMO_ALIASES.pop(key, None)
        _evict_step_memo_over_cap_locked()
    return key


def _step_memo_add_alias(alias_key, primary_key):
    """Make `alias_key` resolve to the same entry as `primary_key` (D7b cause
    A: the completed-step index). A no-op if `primary_key` has since been
    evicted — nothing to alias to — or if the keys already coincide.

    If `alias_key` already pointed at a DIFFERENT primary (e.g. this exact
    (state, completed_step) pair was produced by a different logical step in
    an earlier compile), the alias is repointed here — last write wins, the
    same semantics a plain dict cache would have.
    """
    with _STEP_MEMO_LOCK:
        entry = _STEP_MEMO.get(primary_key)
        if entry is None or alias_key == primary_key:
            return
        old_primary = _STEP_MEMO_ALIASES.get(alias_key)
        if old_primary is not None and old_primary != primary_key:
            old_entry = _STEP_MEMO.get(old_primary)
            if old_entry is not None:
                old_entry["aliases"].discard(alias_key)
        _STEP_MEMO_ALIASES[alias_key] = primary_key
        entry["aliases"].add(alias_key)


def validate_step_world_precondition(step, shared_world, manifest):
    """Validate shared-world requirements before memo lookup or compilation."""
    if step["op"] != "place" or manifest is None:
        return
    destination = step["dest"]
    place_spec = (
        manifest["facilities"].get(destination, {}).get("place") or {})
    if (place_spec.get("requires_open") is not None
            and shared_world["facility_state"].get(destination) != "open"):
        raise ValueError(
            f"container '{destination}' is "
            f"{shared_world['facility_state'].get(destination)!r} at "
            f"place step '{step['id']}'; expected Open skill effect first")


def compile_one_step(
    rig,
    robot_name,
    step,
    *,
    standoffs,
    tracks_dir,
    facilities,
    close_to_open,
    reverse_always,
    context,
    shared_world,
    next_step=None,
    placement_regions=None,
    manifest=None,
):
    """Compile exactly one step while threading explicit robot/world state.

    This is the common geometry primitive for the legacy full-plan loop and
    Compiler V2's chronological cursor.  It deliberately does not assign an
    absolute start time or commit a reservation; those belong to the caller.
    """
    validate_step_world_precondition(step, shared_world, manifest)

    return compile_robot(
        rig, robot_name, [step], standoffs, tracks_dir, facilities,
        close_to_open, reverse_always,
        context=context,
        shared_world=shared_world,
        next_step=next_step,
        placement_regions=placement_regions,
    )


def apply_compiled_step_world_effect(step, shared_world, facilities, manifest):
    """Apply the symbolic facility effect after a step has been accepted."""
    skill_name = _step_skill_name(step)
    skill_facility = facilities.get(skill_name) if skill_name else None
    if skill_facility not in manifest["facilities"]:
        return
    art = manifest["facilities"][skill_facility].get("articulation") or {}
    art_skills = art.get("skills") or {}
    if skill_name == art_skills.get("open"):
        shared_world["facility_state"][skill_facility] = "open"
    elif skill_name == art_skills.get("close"):
        shared_world["facility_state"][skill_facility] = "closed"


def snapshot_compile_state(contexts, shared_world):
    """Deep-copy mutable compiler state for bounded candidate verification."""
    return copy.deepcopy(contexts), copy.deepcopy(shared_world)


def compile_plan_v2(
    scene_xml, plans, standoffs_path, tracks_dir, manifest_path,
    *, repair_enabled=True, progress_callback=None,
):
    """Compile geometry/time jointly and locally repair current navigation."""
    # Local imports avoid the conflict_tools -> skill_generators import cycle.
    from mujoco_skills.orchestrator.compiler_v2_plan import (
        DEFERRED_CLOSE_FACILITY_FIELD,
        DETOUR_OVERRIDES_FIELD,
        DETOUR_PARENT_GROUP_FIELD,
        GENERATED_AFTER_FIELD,
        add_generated_after,
        apply_detour_override,
        mark_automatic_close_owners_deferred,
        mark_generated_step,
        mark_generated_via,
        prepare_compiler_v2_input,
        reroute_authored_via_points,
        select_deferred_close_owner,
    )
    from mujoco_skills.orchestrator.conflict_tools import (
        ToolError,
        analyze_spatial_deadlock,
        apply_add_after,
        apply_insert_go_to,
        apply_insert_yield,
        apply_replan_path,
        compute_go_away_maneuver,
    )

    plan_started = time.perf_counter()
    standoffs = load_standoffs(standoffs_path)
    island = json.loads(Path(standoffs_path).read_text("utf-8"))["island_bbox"]
    manifest = json.loads(Path(manifest_path).read_text("utf-8"))
    skill_robots = {
        skill["name"]: set(skill.get("robots") or [])
        for skill in manifest.get("skills", [])
    }
    detour_overrides = {
        override["id"]: copy.deepcopy(override)
        for override in plans.get(DETOUR_OVERRIDES_FIELD, [])
        if isinstance(override, dict) and override.get("id")
    } if isinstance(plans, dict) else {}
    facilities = {s["name"]: s.get("facility") for s in manifest["skills"]}
    facilities.update({name: name for name in manifest["facilities"]})
    placement_regions = placement_regions_from_manifest(manifest["facilities"])

    close_to_open = {}
    reverse_always = set()
    for spec in manifest["facilities"].values():
        art = spec.get("articulation") or {}
        skills = art.get("skills") or {}
        if skills.get("open") and skills.get("close"):
            close_to_open[skills["close"]] = skills["open"]
            if any("slide" in joint for joint in (art.get("joints") or [])):
                reverse_always.add(skills["close"])

    plans, deferred_close_declarations = mark_automatic_close_owners_deferred(
        plans, manifest)
    plans = flatten_tasks(plans)
    per_facility = {}
    for steps in plans.values():
        for step in steps:
            if step["op"] == "place" and step.get("at") is None:
                anchor = step.get("at_anchor")
                key = (step["dest"], tuple(anchor) if anchor else None)
                per_facility.setdefault(key, []).append(step)
    for placed in per_facility.values():
        for slot, step in enumerate(placed):
            step["_slot"], step["_slot_count"] = slot, len(placed)

    # Materialise container/open/close cross-robot dependencies and validate
    # topology.  V2 intentionally ignores the returned total geometry order;
    # its robot cursors choose the chronological order below.
    _container_dependency_order(plans, manifest)

    # Facility activity completions drive automatic close ownership. Include
    # both destination placements and source transfers: a cabinet emptied onto
    # an island must stay open until every cabinet object's entire move group
    # has completed, then close on the actual latest eligible robot just like a
    # fridge receiving objects.
    placement_completions = {}
    for placement_robot, robot_steps in plans.items():
        for placement_index, placement in enumerate(robot_steps):
            if placement.get("op") != "place" or not placement.get("dest"):
                continue
            completion = placement
            if (placement_index + 1 < len(robot_steps)
                    and robot_steps[placement_index + 1].get("op") == "reset"):
                completion = robot_steps[placement_index + 1]
            completion_entry = {
                "step": completion["id"],
                "place_step": placement["id"],
                "robot": placement_robot,
            }
            destination = placement["dest"]
            placement_completions.setdefault(destination, []).append(
                completion_entry)

            group = placement.get("group")
            source_object = next((
                candidate.get("object")
                for candidate in reversed(robot_steps[:placement_index])
                if candidate.get("group") == group
                and candidate.get("op") == "pick"
                and candidate.get("object")
            ), None)
            source = (
                (manifest["objects"].get(source_object) or {}).get(
                    "home_facility")
                if source_object is not None else None
            )
            if source and source != destination:
                placement_completions.setdefault(source, []).append(
                    {**completion_entry, "source_object": source_object})

    # Unlocked close groups are deliberately removed from their provisional
    # robot programs. They are inserted at the winning robot's live cursor only
    # after every placement completion has a real scheduled end time.
    deferred_close_groups = []
    for declaration in deferred_close_declarations:
        group_id = declaration["close_task"]
        group_steps = []
        for provisional_robot, robot_steps in plans.items():
            kept = []
            for program_step in robot_steps:
                if (program_step.get("group") == group_id
                        and program_step.get(DEFERRED_CLOSE_FACILITY_FIELD)
                        == declaration["facility"]):
                    group_steps.append(program_step)
                else:
                    kept.append(program_step)
            plans[provisional_robot] = kept
        if not group_steps:
            continue
        group_steps.sort(key=lambda value: (
            value.get("_author_order", 0), value["id"]))
        deferred_close_groups.append({
            **declaration,
            "steps": group_steps,
            "placement_completions": copy.deepcopy(
                placement_completions.get(declaration["facility"], [])),
            "eligible_robots": skill_robots.get(
                (((manifest["facilities"][declaration["facility"]]
                   .get("articulation") or {}).get("skills") or {})
                .get("close")),
                set(),
            ) & set(plans),
            "author_order": group_steps[0].get("_author_order", 0),
        })
    close_owner_assignments = []

    def emit_progress():
        if progress_callback is None:
            return
        total = sum(len(steps) for steps in plans.values()) + sum(
            len(group["steps"]) for group in deferred_close_groups)
        progress_callback(len(commit_order), total)

    rigs = {
        robot: get_rig(
            scene_xml, int(robot.replace("robot", "")), island=island)
        for robot in plans
    }
    for rig in rigs.values():
        rig.set_ready(_ready_from_scene_initial(rig, manifest))
    first_rig = next(iter(rigs.values()))
    initial_q = {
        robot: _scene_initial_qpos(rig, manifest)
        for robot, rig in rigs.items()
    }
    shared_world = _make_shared_world(
        first_rig, manifest, initial_q=initial_q[next(iter(rigs))])
    contexts = {
        robot: _new_robot_compile_context(rig, initial_q=initial_q[robot])
        for robot, rig in rigs.items()
    }
    def refresh_program_metadata():
        following_by_id = {}
        for robot_steps in plans.values():
            for order, program_step in enumerate(robot_steps):
                program_step["_robot_order"] = order
            for current, following in zip(robot_steps, robot_steps[1:]):
                following_by_id[current["id"]] = following
        return following_by_id

    next_step = refresh_program_metadata()

    all_program_lists = [
        *plans.values(),
        *(group["steps"] for group in deferred_close_groups),
    ]
    all_ids = {
        step["id"] for robot_steps in all_program_lists for step in robot_steps}
    for robot_steps in all_program_lists:
        for step in robot_steps:
            unknown = sorted(
                dependency for dependency in step.get("after", [])
                if dependency not in all_ids)
            if unknown:
                raise ValueError(
                    f"step '{step['id']}' depends on unknown step id(s): {unknown}")

    anchors = []
    rest_points = {}
    for robot, rig in rigs.items():
        # Anonymous navigate steps (including go_to_rest) interpret their
        # explicit standoff as a chassis/yaw-pivot coordinate. Store and
        # reserve the same physical point here; using base_xy would shift an
        # Omron by the arm-mount offset when it "returns" home.
        xy = [float(value) for value in rig.chassis_xy(rig.model.qpos0)]
        rest_points[robot] = [round(value, 3) for value in xy]
        anchor_id = f"__compiler_v2_initial__{robot}"
        anchors.append({
            "id": anchor_id, "robot": robot, "start": 0.0, "order": -1,
            "duration": 0.0, "base_xy": xy,
            "base_trace": [[0.0, *xy], [0.0, *xy]], "after": [],
            "facility": None, "object": None, "place_xyz": None,
            "label": anchor_id, "group": anchor_id,
            "completed_step": {"id": anchor_id, "op": "reset", "group": anchor_id},
        })

    def dependency_graph():
        return {
            program_step["id"]: list(program_step.get("after", []))
            for robot_steps in plans.values() for program_step in robot_steps
        }

    def complete_dependency_graph(programs):
        dependencies = {
            program_step["id"]: set(program_step.get("after", []))
            for robot_steps in programs.values()
            for program_step in robot_steps
        }
        for robot_steps in programs.values():
            for previous, current in zip(robot_steps, robot_steps[1:]):
                dependencies.setdefault(current["id"], set()).add(
                    previous["id"])
        return dependencies

    def dependency_cycle_for_wait(step_id, after_step_id):
        """Preflight an add-after candidate against the complete plan DAG.

        This mirrors Resolver V2's candidate pruning: authored/container
        dependencies and implicit per-robot program order are considered
        before a repair tool is attempted.  ``apply_add_after`` keeps its own
        cycle check as a defensive backstop.
        """
        if not after_step_id:
            return None
        dependencies = complete_dependency_graph(plans)
        dependencies.setdefault(step_id, set()).add(after_step_id)
        return _find_dependency_cycle(dependencies)

    def tool_plan_and_compile_result(
            candidate_items, *, unfinished_robots=None):
        tool_plan = copy.deepcopy(plans)
        completed_by_id = {
            item["id"]: item.get("completed_step") or {}
            for item in [*items, *candidate_items]
        }
        for robot_steps in tool_plan.values():
            for tool_step in robot_steps:
                completed_step = completed_by_id.get(tool_step["id"])
                if completed_step:
                    # Resolver geometry tools need compiled destination/route
                    # fields. Tool mutations are copied back selectively, so
                    # these temporary defaults never become authored pins.
                    tool_step.update(copy.deepcopy(completed_step))
        probe_items = [*anchors, *items, *candidate_items]
        return tool_plan, {
            "scene_xml": str(Path(scene_xml).resolve()),
            "base_timelines": serialize_base_occupancy_timelines(
                probe_items, unfinished_robots=unfinished_robots),
            "completed": {
                robot: [
                    copy.deepcopy(completed_by_id.get(step["id"], step))
                    for step in robot_steps
                ]
                for robot, robot_steps in plans.items()
            },
        }

    def other_party(conflict, current_step_id):
        return next((
            party for party in conflict.get("parties", [])
            if party.get("step") != current_step_id
        ), None)

    def next_departure_step(party):
        if not party:
            return None
        other_robot = party.get("robot")
        robot_steps = plans.get(other_robot) or []
        party_step = party.get("step")
        known_ids = {
            step["id"] for steps in plans.values() for step in steps}
        if party.get("motion") == "moving" and party_step in known_ids:
            return party_step
        start_index = cursors.get(other_robot, 0)
        for index, program_step in enumerate(robot_steps):
            if program_step.get("id") == party_step:
                start_index = max(start_index, index + 1)
                break
        remaining = robot_steps[start_index:]
        departure = next((
            program_step["id"] for program_step in robot_steps[start_index:]
            if (
                program_step.get("op") in MOVING_OPS
                # Decomposed move tasks end with a short base retreat.  It is
                # the next real departure from the just-used facility even
                # though reset is not itself a repairable navigation step.
                or (
                    program_step.get("op") == "reset"
                    and float(program_step.get("retreat") or 0.0) > 0.0
                )
            )
        ), None)
        if departure is not None:
            return departure
        # The robot may still have a place/close/etc. to finish at this pose
        # before it becomes terminal and eligible for go_to_rest.  Waiting for
        # that last local step advances the frontier; waiting for the already-
        # committed dwell item does not.
        return remaining[-1]["id"] if remaining else None

    def unfinished_robots_after_candidate(candidate_robot):
        """Return robots whose trailing dwell is an incremental frontier."""
        return {
            name for name, robot_steps in plans.items()
            if cursors.get(name, 0) + (1 if name == candidate_robot else 0)
            < len(robot_steps)
        }

    cursors = {robot: 0 for robot in plans}
    robot_end = {robot: 0.0 for robot in plans}
    completed_end = {}
    items = []
    commit_order = []
    reservation_checks = 0
    repair_ledger = []
    attempted = set()
    checkpoints = []
    memo_mode = _compile_memo_mode()
    memo_generation = (
        compile_memo_generation(
            scene_xml, standoffs_path, tracks_dir, manifest_path)
        if memo_mode != "off" else None
    )
    memo_hits = memo_misses = memo_uncacheable = 0
    # Successful final-frontier steps only. Failed reservation candidates are
    # useful primary cache entries, but must never create completed-plan aliases.
    memo_committed = []

    # The initial generic activity is emitted by the orchestrator before the
    # HTTP compile begins. This first concrete count replaces it as soon as V2
    # has flattened the plan and accounted for deferred close groups.
    emit_progress()

    def bind_ready_deferred_closes():
        """Insert ready close groups at the actual last placer's cursor."""
        nonlocal next_step
        ready = []
        for group in deferred_close_groups:
            winner = select_deferred_close_owner(
                group["placement_completions"], completed_end,
                group["eligible_robots"])
            if winner is None:
                continue
            winner_robot = winner["robot"]
            winner_cursor = cursors[winner_robot]
            winner_steps = plans[winner_robot]
            # Never split a close group that was bound on an earlier pass.
            if (winner_cursor < len(winner_steps)
                    and winner_steps[winner_cursor].get(
                        "_compiler_v2_deferred_close_bound")):
                continue
            ready.append((group, winner))
        if not ready:
            return False

        # Bind every currently-ready group without reversing groups that share
        # one winner cursor. Earlier actual completion, then author order, wins.
        by_robot = {}
        for group, winner in ready:
            by_robot.setdefault(winner["robot"], []).append((group, winner))
        bound_group_ids = set()
        for winner_robot, bindings in sorted(by_robot.items()):
            bindings.sort(key=lambda value: (
                float(completed_end[value[1]["step"]]),
                value[0]["author_order"],
                str(value[0]["close_task"]),
            ))
            insertion = cursors[winner_robot]
            inserted_steps = []
            for group, winner in bindings:
                for program_step in group["steps"]:
                    program_step["_compiler_v2_deferred_close_bound"] = True
                    inserted_steps.append(program_step)
                close_owner_assignments.append({
                    "facility": group["facility"],
                    "close_task": group["close_task"],
                    "from_robot": group["provisional_robot"],
                    "to_robot": winner_robot,
                    "changed": group["provisional_robot"] != winner_robot,
                    "placement_step": winner["place_step"],
                    "placement_completion_step": winner["step"],
                    "placement_completion_time": round(
                        float(completed_end[winner["step"]]), 3),
                })
                bound_group_ids.add(id(group))
            plans[winner_robot][insertion:insertion] = inserted_steps
        deferred_close_groups[:] = [
            group for group in deferred_close_groups
            if id(group) not in bound_group_ids
        ]
        next_step = refresh_program_metadata()
        return True

    def actual_step(step_id):
        for robot_steps in plans.values():
            for program_step in robot_steps:
                if program_step.get("id") == step_id:
                    return program_step
        raise KeyError(step_id)

    while (any(cursors[robot] < len(plans[robot]) for robot in plans)
           or deferred_close_groups):
        bind_ready_deferred_closes()
        if (not any(cursors[robot] < len(plans[robot]) for robot in plans)
                and deferred_close_groups):
            waiting = [group["close_task"] for group in deferred_close_groups]
            raise ValueError(
                "deferred close groups never became ready: " + ", ".join(waiting))
        start, robot, step = _select_incremental_ready(
            plans, cursors, robot_end, completed_end)
        contexts_before, shared_world_before = snapshot_compile_state(
            contexts, shared_world)
        checkpoint = {
            "step_id": step["id"],
            "contexts": contexts_before,
            "shared_world": shared_world_before,
            "items_len": len(items),
            "cursors": dict(cursors),
            "robot_end": dict(robot_end),
            "completed_end": dict(completed_end),
            "commit_order": list(commit_order),
            "memo_committed_len": len(memo_committed),
        }
        memo_material = None
        memo_key = None
        cached = None
        if memo_mode != "off":
            try:
                memo_material = _step_memo_key_material(
                    memo_generation,
                    contexts[robot],
                    shared_world,
                    step,
                    next_step.get(step["id"]),
                )
            except (TypeError, ValueError):
                memo_uncacheable += 1
            else:
                memo_key = memo_material["key"]
                cached = _step_memo_get(memo_key)

        resolved_primary_key = None
        if cached is not None and memo_mode == "on":
            compiled_items = cached["items"]
            _patch_v2_memo_hit_metadata(compiled_items, step)
            contexts[robot] = cached["context"]
            shared_world = cached["shared_world"]
            resolved_primary_key = cached["primary_key"]
            memo_hits += 1
        else:
            compiled_items = compile_one_step(
                rigs[robot], robot, step,
                standoffs=standoffs,
                tracks_dir=tracks_dir,
                facilities=facilities,
                close_to_open=close_to_open,
                reverse_always=reverse_always,
                context=contexts[robot],
                shared_world=shared_world,
                next_step=next_step.get(step["id"]),
                placement_regions=placement_regions,
                manifest=manifest,
            )
            if memo_mode != "off":
                memo_misses += 1
            if memo_mode == "verify" and cached is not None:
                if not _memo_values_equal(
                    _memo_items_for_verify(cached["items"], step),
                    _memo_items_for_verify(compiled_items, step),
                ):
                    raise RuntimeError(
                        "MJSKILL_COMPILE_MEMO=verify: Compiler V2 cached "
                        f"items for step '{step['id']}' diverge from a fresh "
                        "compile")
                if not _memo_values_equal(
                        cached["context"], contexts[robot]):
                    raise RuntimeError(
                        "MJSKILL_COMPILE_MEMO=verify: Compiler V2 cached "
                        f"post-context for step '{step['id']}' diverges from "
                        "a fresh compile")
                if not _memo_values_equal(
                        cached["shared_world"], shared_world):
                    raise RuntimeError(
                        "MJSKILL_COMPILE_MEMO=verify: Compiler V2 cached "
                        f"shared-world after step '{step['id']}' diverges "
                        "from a fresh compile")
            if memo_key is not None:
                resolved_primary_key = _step_memo_put(
                    memo_key, compiled_items, contexts[robot], shared_world)
        cursor_time = start
        for item in compiled_items:
            item["start"] = round(cursor_time, 3)
            cursor_time += float(item["duration"])
            _validate_track_robot(item)

        # A terminal non-moving step can create a new final-dwell reservation
        # *after* another robot's navigation was already accepted.  The old
        # loop only queried reservations while compiling a moving candidate,
        # so this late occupancy reached the final residual check with no
        # chance to repair it.  Recheck the terminal candidate here, create a
        # real departure anchor for its robot, make the blocked navigation wait
        # for that departure, and roll back to the navigation's checkpoint.
        terminal_departure_applied = False
        if (step["op"] not in MOVING_OPS
                and len(plans) > 1
                and cursors[robot] + 1 >= len(plans[robot])
                and not deferred_close_groups):
            terminal_conflicts = [
                conflict
                for conflict in _incremental_candidate_conflicts(
                    anchors, items, compiled_items, step["id"],
                    unfinished_robots=(
                        unfinished_robots_after_candidate(robot)))
                if conflict.get("requires_departure_anchor")
                and conflict.get("class") == "one_moving"
            ]
            if terminal_conflicts:
                reservation_checks += 1
                focus = terminal_conflicts[0]
                final_party = next((
                    party for party in focus.get("parties", [])
                    if party.get("occupancy") == "final_dwell"
                ), None)
                moving_party = next((
                    party for party in focus.get("parties", [])
                    if party.get("motion") == "moving"
                ), None)
                occupant_robot = (
                    final_party.get("robot") if final_party else None)
                moving_step_id = (
                    moving_party.get("step") if moving_party else None)
                departure_key = (
                    "late_final_dwell", occupant_robot, moving_step_id)
                if (repair_enabled and final_party and moving_party
                        and occupant_robot == robot
                        and moving_step_id in commit_order
                        and departure_key not in attempted):
                    attempted.add(departure_key)
                    try:
                        candidate_plan = copy.deepcopy(plans)
                        old_len = len(candidate_plan[occupant_robot])
                        departure_id = apply_insert_go_to(
                            candidate_plan, occupant_robot,
                            rest_points.get(occupant_robot))
                        for generated_step in candidate_plan[occupant_robot][
                                old_len:]:
                            mark_generated_step(generated_step, "go_to_rest")
                        moving_copy = next(
                            candidate
                            for robot_steps in candidate_plan.values()
                            for candidate in robot_steps
                            if candidate["id"] == moving_step_id)
                        candidate_dependencies = {
                            candidate["id"]: list(candidate.get("after", []))
                            for robot_steps in candidate_plan.values()
                            for candidate in robot_steps
                        }
                        apply_add_after(
                            candidate_plan, candidate_dependencies,
                            moving_step_id, departure_id)
                        add_generated_after(moving_copy, departure_id)
                        rollback_index = next(
                            index for index, saved in enumerate(checkpoints)
                            if saved["step_id"] == moving_step_id)
                        saved = checkpoints[rollback_index]
                    except (ToolError, StopIteration) as exc:
                        repair_ledger.append({
                            "tool": "late_final_dwell_departure",
                            "step": moving_step_id,
                            "robot": occupant_robot,
                            "accepted": False,
                            "reason": str(exc),
                        })
                    else:
                        plans = candidate_plan
                        contexts = saved["contexts"]
                        shared_world = saved["shared_world"]
                        items = items[:saved["items_len"]]
                        cursors = dict(saved["cursors"])
                        robot_end = dict(saved["robot_end"])
                        completed_end = dict(saved["completed_end"])
                        commit_order = list(saved["commit_order"])
                        memo_committed = memo_committed[
                            :saved["memo_committed_len"]]
                        checkpoints = checkpoints[:rollback_index]
                        next_step = refresh_program_metadata()
                        repair_ledger.append({
                            "tool": "late_final_dwell_departure",
                            "step": moving_step_id,
                            "after": departure_id,
                            "robot": occupant_robot,
                            "accepted": True,
                        })
                        terminal_departure_applied = True
                elif not repair_enabled:
                    raise IncrementalCompileConflict(
                        step["id"], terminal_conflicts,
                        list(commit_order), repair_ledger)
        if terminal_departure_applied:
            continue

        if step["op"] in MOVING_OPS:
            reservation_checks += 1
            unfinished_robots = unfinished_robots_after_candidate(robot)
            relevant = _incremental_candidate_conflicts(
                anchors, items, compiled_items, step["id"],
                unfinished_robots=unfinished_robots)
            if relevant:
                if not repair_enabled:
                    raise IncrementalCompileConflict(
                        step["id"], relevant, list(commit_order), repair_ledger)

                # A facility overlap may be listed before the path detector's
                # more informative final-dwell record for the same candidate.
                # Prefer the latter: it identifies the next real departure to
                # wait for, or proves go_to_rest mandatory when none remains.
                focus = next((
                    conflict for conflict in relevant
                    if conflict.get("requires_departure_anchor")
                ), relevant[0])
                party = other_party(focus, step["id"])
                other_robot = party.get("robot") if party else None
                conflict_key = (
                    step["id"], party.get("step") if party else None,
                    focus.get("kind"),
                )

                # Required terminal go-away: waiting cannot clear a robot whose
                # real program cursor is exhausted at its final dwell.
                finished_other = (
                    other_robot in plans
                    and cursors[other_robot] >= len(plans[other_robot])
                )
                departure_key = ("go_to_rest", *conflict_key)
                if (party and finished_other
                        and party.get("occupancy") == "final_dwell"
                        and departure_key not in attempted):
                    attempted.add(departure_key)
                    try:
                        candidate_plan = copy.deepcopy(plans)
                        old_len = len(candidate_plan[other_robot])
                        departure_id = apply_insert_go_to(
                            candidate_plan, other_robot,
                            rest_points.get(other_robot))
                        for generated_step in candidate_plan[other_robot][old_len:]:
                            mark_generated_step(generated_step, "go_to_rest")
                        candidate_step = next(
                            candidate
                            for candidate in candidate_plan[robot]
                            if candidate["id"] == step["id"])
                        apply_add_after(
                            candidate_plan, dependency_graph(),
                            step["id"], departure_id)
                        add_generated_after(candidate_step, departure_id)
                    except ToolError as exc:
                        repair_ledger.append({
                            "tool": "go_to_rest", "step": step["id"],
                            "accepted": False, "reason": str(exc),
                        })
                    else:
                        plans = candidate_plan
                        repair_ledger.append({
                            "tool": "go_to_rest", "step": step["id"],
                            "robot": other_robot, "accepted": True,
                        })
                        contexts, shared_world = contexts_before, shared_world_before
                        next_step = refresh_program_metadata()
                        continue

                tool_plan, local_compile = tool_plan_and_compile_result(
                    compiled_items, unfinished_robots=unfinished_robots)

                # Determine the temporal candidate before choosing a repair.
                # Resolver V2 prunes a candidate edge against the full DAG;
                # Compiler V2 must do the same instead of learning about an
                # indirect semantic cycle only when apply_add_after rejects it.
                wait_anchor = next_departure_step(party)
                wait_cycle = dependency_cycle_for_wait(
                    step["id"], wait_anchor)

                # Required temporary go-away: reuse the current deadlock proof
                # and roll back only the overlapping committed frontier.
                yield_applied = False
                if focus.get("class") == "both_moving":
                    yield_key = ("yield", *conflict_key)
                    if yield_key not in attempted:
                        attempted.add(yield_key)
                        try:
                            analysis = analyze_spatial_deadlock(
                                tool_plan, focus, local_compile)
                            maneuver = next(iter(
                                analysis.get("yield_candidates") or []), None)
                            if not analysis.get("detected") or maneuver is None:
                                raise ToolError("priority ordering remains feasible")
                            yield_id = apply_insert_yield(
                                tool_plan,
                                maneuver["yielding_step"],
                                maneuver["winner_step"],
                                focus,
                                local_compile,
                            )
                            affected = {
                                maneuver["yielding_step"], maneuver["winner_step"]}
                            rollback_index = next(
                                index for index, saved in enumerate(checkpoints)
                                if saved["step_id"] in affected)
                            saved = checkpoints[rollback_index]

                            yielding_actual = actual_step(maneuver["yielding_step"])
                            yielding_robot = maneuver["yielding_robot"]
                            insertion_index = plans[yielding_robot].index(yielding_actual)
                            tool_yield = next(
                                candidate for candidate in tool_plan[yielding_robot]
                                if candidate["id"] == yield_id)
                            generated_yield = copy.deepcopy(tool_yield)
                            mark_generated_step(generated_yield, "yield")
                            plans[yielding_robot].insert(
                                insertion_index, generated_yield)

                            for affected_id in affected:
                                live = actual_step(affected_id)
                                tool_version = next(
                                    candidate
                                    for robot_steps in tool_plan.values()
                                    for candidate in robot_steps
                                    if candidate["id"] == affected_id)
                                old_after = set(live.get("after", []))
                                for dependency in tool_version.get("after", []):
                                    if dependency not in old_after:
                                        add_generated_after(live, dependency)
                                mark_generated_via(
                                    live, tool_version.get("via_points"))

                            contexts = saved["contexts"]
                            shared_world = saved["shared_world"]
                            items = items[:saved["items_len"]]
                            cursors = dict(saved["cursors"])
                            robot_end = dict(saved["robot_end"])
                            completed_end = dict(saved["completed_end"])
                            commit_order = list(saved["commit_order"])
                            memo_committed = memo_committed[
                                :saved["memo_committed_len"]]
                            checkpoints = checkpoints[:rollback_index]
                            next_step = refresh_program_metadata()
                        except (ToolError, StopIteration) as exc:
                            repair_ledger.append({
                                "tool": "yield", "step": step["id"],
                                "accepted": False, "reason": str(exc),
                            })
                        else:
                            repair_ledger.append({
                                "tool": "yield", "step": step["id"],
                                "yielding_step": maneuver["yielding_step"],
                                "winner_step": maneuver["winner_step"],
                                "accepted": True,
                            })
                            yield_applied = True
                if yield_applied:
                    continue

                # A topology-invalid wait means the occupying robot's planned
                # departure is downstream of this navigation (commonly a
                # container Close waiting for this object's placement). Move
                # the occupant away first, then let its original program --
                # including the return/Close -- continue afterward. This is
                # the incremental counterpart of Resolver V2's departure-
                # prerequisite candidate.
                go_away_key = ("detour", step["id"], wait_anchor)
                if (wait_cycle and party and other_robot in plans
                        and go_away_key not in attempted):
                    attempted.add(go_away_key)
                    try:
                        candidate_plan = copy.deepcopy(plans)
                        current_copy = next(
                            candidate
                            for candidate in candidate_plan[robot]
                            if candidate["id"] == step["id"])
                        anchor_copy = next((
                            candidate
                            for robot_steps in candidate_plan.values()
                            for candidate in robot_steps
                            if candidate["id"] == wait_anchor
                        ), None)
                        replaced_reverse_wait = False
                        # Replace a previous compiler wait pointing in the
                        # opposite direction. Authored/container edges are
                        # never removed; an indirect semantic cycle therefore
                        # reaches go-away with its causal topology intact.
                        if (anchor_copy is not None
                                and step["id"] in set(anchor_copy.get(
                                    GENERATED_AFTER_FIELD) or [])):
                            anchor_copy["after"] = [
                                dependency
                                for dependency in anchor_copy.get("after", [])
                                if dependency != step["id"]
                            ]
                            anchor_copy[GENERATED_AFTER_FIELD] = [
                                dependency for dependency in anchor_copy.get(
                                    GENERATED_AFTER_FIELD, [])
                                if dependency != step["id"]
                            ]
                            if not anchor_copy["after"]:
                                anchor_copy.pop("after", None)
                            if not anchor_copy[GENERATED_AFTER_FIELD]:
                                anchor_copy.pop(GENERATED_AFTER_FIELD, None)
                            replaced_reverse_wait = True
                        insertion_index = cursors[other_robot]
                        if insertion_index >= len(candidate_plan[other_robot]):
                            raise ToolError(
                                "cyclic wait occupant has no resumable program "
                                "frontier")

                        future_navigation = next((
                            candidate
                            for candidate in candidate_plan[other_robot][
                                insertion_index:]
                            if candidate.get("op") in MOVING_OPS
                        ), None)
                        future_goal = None
                        if future_navigation is not None:
                            if future_navigation.get("standoff") is not None:
                                future_goal = future_navigation["standoff"]
                            else:
                                target = future_navigation.get("target")
                                if target in standoffs:
                                    future_goal = standoffs[target]["standoff_xy"]
                        maneuver = compute_go_away_maneuver(
                            tool_plan,
                            other_robot,
                            step["id"],
                            focus,
                            local_compile,
                            resume_goal=future_goal,
                        )
                        parking = maneuver["parking_xy"]
                        go_away_id = (
                            f"{step['id']}#detour_{other_robot}")
                        if any(
                                candidate.get("id") == go_away_id
                                for robot_steps in candidate_plan.values()
                                for candidate in robot_steps):
                            raise ToolError(
                                f"detour step {go_away_id!r} already exists")

                        following = candidate_plan[other_robot][insertion_index]
                        go_away_step = {
                            "id": go_away_id,
                            "op": "navigate",
                            "target": None,
                            "standoff": [
                                round(float(parking[0]), 3),
                                round(float(parking[1]), 3),
                            ],
                            "via_points": [
                                [round(float(point[0]), 3),
                                 round(float(point[1]), 3)]
                                for point in maneuver["departure_route"][1:-1]
                            ],
                            "align_final_yaw": False,
                            "group": go_away_id,
                            DETOUR_PARENT_GROUP_FIELD: following.get("group"),
                            "_author_order": (
                                float(following.get("_author_order", 0)) - 0.5
                            ),
                        }
                        mark_generated_step(go_away_step, "detour")
                        apply_detour_override(go_away_step, detour_overrides)
                        candidate_plan[other_robot].insert(
                            insertion_index, go_away_step)
                        add_generated_after(current_copy, go_away_id)
                        candidate_cycle = _find_dependency_cycle(
                            complete_dependency_graph(candidate_plan))
                        if candidate_cycle:
                            raise ToolError(
                                "detour would create a dependency cycle: "
                                + " -> ".join(candidate_cycle))
                    except (ToolError, StopIteration, ValueError) as exc:
                        repair_ledger.append({
                            "tool": "detour", "step": step["id"],
                            "after": wait_anchor, "robot": other_robot,
                            "cycle": wait_cycle,
                            "accepted": False, "reason": str(exc),
                        })
                    else:
                        plans = candidate_plan
                        if replaced_reverse_wait:
                            # The edge was accepted under an earlier frontier
                            # and has now been deliberately replaced. Permit
                            # that same temporal candidate to be reconsidered
                            # after the go-away has changed the topology.
                            attempted.discard((
                                "wait", wait_anchor, step["id"]))
                        repair_ledger.append({
                            "tool": "detour", "step": step["id"],
                            "detour_step": go_away_id,
                            "winner_step": step["id"],
                            "robot": other_robot,
                            "parking_xy": [
                                round(float(parking[0]), 3),
                                round(float(parking[1]), 3),
                            ],
                            "resume_goal": (
                                [round(float(future_goal[0]), 3),
                                 round(float(future_goal[1]), 3)]
                                if future_goal is not None else None
                            ),
                            "cycle": wait_cycle,
                            "accepted": True,
                        })
                        contexts, shared_world = (
                            contexts_before, shared_world_before)
                        next_step = refresh_program_metadata()
                        continue

                # Ordinary path conflict: one deterministic reroute attempt.
                reroute_key = ("reroute", *conflict_key)
                if ("replan_path" in focus.get("allowed_tools", [])
                        and other_robot and reroute_key not in attempted):
                    attempted.add(reroute_key)
                    try:
                        tool_plan, local_compile = tool_plan_and_compile_result(
                            compiled_items,
                            unfinished_robots=unfinished_robots)
                        apply_replan_path(
                            tool_plan, step["id"], other_robot,
                            focus, local_compile,
                            authored_via_points=reroute_authored_via_points(step),
                        )
                        tool_step = next(
                            candidate for candidate in tool_plan[robot]
                            if candidate["id"] == step["id"])
                        mark_generated_via(step, tool_step.get("via_points"))
                    except ToolError as exc:
                        repair_ledger.append({
                            "tool": "reroute", "step": step["id"],
                            "accepted": False, "reason": str(exc),
                        })
                    else:
                        repair_ledger.append({
                            "tool": "reroute", "step": step["id"],
                            "avoid": other_robot, "accepted": True,
                        })
                        contexts, shared_world = contexts_before, shared_world_before
                        continue

                # Conservative temporal serialization. The candidate was
                # already proven topology-safe above; apply_add_after repeats
                # the check defensively before mutating the live plan.
                wait_key = ("wait", step["id"], wait_anchor)
                if wait_anchor and wait_key not in attempted:
                    attempted.add(wait_key)
                    try:
                        apply_add_after(
                            plans, dependency_graph(), step["id"], wait_anchor)
                        add_generated_after(step, wait_anchor)
                    except ToolError as exc:
                        repair_ledger.append({
                            "tool": "wait", "step": step["id"],
                            "after": wait_anchor, "accepted": False,
                            "reason": str(exc),
                        })
                    else:
                        repair_ledger.append({
                            "tool": "wait", "step": step["id"],
                            "after": wait_anchor, "accepted": True,
                        })
                        contexts, shared_world = contexts_before, shared_world_before
                        continue

                raise IncrementalCompileConflict(
                    step["id"], relevant, list(commit_order), repair_ledger)

        items.extend(compiled_items)
        commit_order.append(step["id"])
        completed_end[step["id"]] = round(cursor_time, 3)
        robot_end[robot] = round(cursor_time, 3)
        cursors[robot] += 1
        apply_compiled_step_world_effect(step, shared_world, facilities, manifest)
        if (resolved_primary_key is not None
                and memo_material is not None and compiled_items):
            completed_step = compiled_items[-1].get("completed_step")
            if isinstance(completed_step, dict):
                try:
                    completed_canon = _memo_canonical_step_v2(completed_step)
                except (TypeError, ValueError):
                    completed_canon = None
                if completed_canon is not None:
                    memo_committed.append({
                        "step_id": step["id"],
                        "robot": robot,
                        "next_step_id": (
                            next_step.get(step["id"]) or {}).get("id"),
                        "context_digest": memo_material["context_digest"],
                        "world_digest": memo_material["world_digest"],
                        "completed_canon": completed_canon,
                        "primary_key": resolved_primary_key,
                    })
        emit_progress()
        checkpoints.append(checkpoint)

    completed = {robot: [] for robot in plans}
    for item in sorted(items, key=lambda value: (
            value["robot"], value["order"])):
        completed[item["robot"]].append(item["completed_step"])
    conflicts = detect_conflicts(items)
    residual = [
        conflict for conflict in conflicts
        if conflict.get("kind") in ("path", "facility")
    ]
    if residual:
        blocked_step = residual[0].get("blocked") or residual[0]["steps"][-1]
        raise IncrementalCompileConflict(
            blocked_step, residual, commit_order, repair_ledger)

    # Register completed-form aliases only after the final repaired plan has
    # passed residual-conflict validation. This avoids aliases whose successor
    # existed only on a frontier later discarded by rollback.
    if memo_mode != "off":
        committed_by_id = {
            record["step_id"]: record for record in memo_committed}
        completed_by_id = {
            item["id"]: item.get("completed_step")
            for item in items
            if isinstance(item.get("completed_step"), dict)
        }
        for robot_steps in plans.values():
            for index, program_step in enumerate(robot_steps):
                record = committed_by_id.get(program_step["id"])
                if record is None:
                    continue
                final_next_id = (
                    robot_steps[index + 1]["id"]
                    if index + 1 < len(robot_steps) else None)
                if record["next_step_id"] != final_next_id:
                    continue
                next_completed_canon = "null"
                if index + 1 < len(robot_steps):
                    following = completed_by_id.get(
                        robot_steps[index + 1]["id"])
                    if following is None:
                        continue
                    try:
                        next_completed_canon = _memo_canonical_step_v2(following)
                    except (TypeError, ValueError):
                        continue
                alias_key = _step_memo_alias_key(
                    memo_generation,
                    record["context_digest"],
                    record["world_digest"],
                    record["completed_canon"],
                    next_completed_canon,
                )
                _step_memo_add_alias(alias_key, record["primary_key"])

        # Compound turns run prepare_compiler_v2_input before recompiling.
        # Register the cleaned completed shape as a second alias when cleanup
        # did not change this step's actual successor. If a detour/rest step
        # was removed between them, the lookahead premise changed and the
        # conservative miss is required.
        prepared_completed, _ = prepare_compiler_v2_input({
            robot: [copy.deepcopy(completed_by_id[step["id"]])
                    for step in robot_steps]
            for robot, robot_steps in plans.items()
        })
        for robot, prepared_steps in prepared_completed.items():
            if not isinstance(prepared_steps, list):
                continue
            for index, prepared_step in enumerate(prepared_steps):
                record = committed_by_id.get(prepared_step.get("id"))
                if record is None:
                    continue
                prepared_next = (
                    prepared_steps[index + 1].get("id")
                    if index + 1 < len(prepared_steps) else None)
                if record["next_step_id"] != prepared_next:
                    continue
                try:
                    prepared_canon = _memo_canonical_step_v2(prepared_step)
                    prepared_next_canon = (
                        _memo_canonical_step_v2(prepared_steps[index + 1])
                        if index + 1 < len(prepared_steps) else "null")
                except (TypeError, ValueError):
                    continue
                alias_key = _step_memo_alias_key(
                    memo_generation,
                    record["context_digest"],
                    record["world_digest"],
                    prepared_canon,
                    prepared_next_canon,
                )
                _step_memo_add_alias(alias_key, record["primary_key"])

    return {
        "items": items,
        "conflicts": conflicts,
        "warnings": [conflict["message"] for conflict in conflicts],
        "completed": completed,
        "rest_points": rest_points,
        "compiler_v2": {
            "commit_order": commit_order,
            "completed_end": completed_end,
            "reservation_checks": reservation_checks,
            "repairs": repair_ledger,
            "close_owner_assignments": close_owner_assignments,
            "memo": {
                "mode": memo_mode,
                "hits": memo_hits,
                "misses": memo_misses,
                "uncacheable": memo_uncacheable,
            },
            "compile_time_sec": round(time.perf_counter() - plan_started, 3),
            "mode": "repair" if repair_enabled else "no_repair",
        },
    }


def compile_plan_v2_no_repair(
    scene_xml, plans, standoffs_path, tracks_dir, manifest_path,
    *, progress_callback=None,
):
    return compile_plan_v2(
        scene_xml, plans, standoffs_path, tracks_dir, manifest_path,
        repair_enabled=False,
        progress_callback=progress_callback,
    )


def compile_plan(
    scene_xml,
    plans,
    standoffs_path,
    tracks_dir,
    manifest_path,
    *,
    scheduler_mode="v1",
    progress_callback=None,
):
    """Compile a per-robot plan into a scheduled list of track items + warnings.

    plans: {"robot0": [step, ...], "robot1": [...]}, each step a dict:
        {"op":"navigate", "target": <object|facility name>}
        {"op":"pick",     "object": <name>}
        {"op":"place",    "object": <name>, "dest": <facility>}
        {"op":"wait",     "duration": <seconds>}
        {"op":"skill",    "name": <articulation skill, e.g. OpenFridge>}
    Any step may add "id": <str> and "after": [id, ...] (cross-robot deps).
    Returns {"items":[...with start...], "warnings":[...]}.
    """
    if scheduler_mode == "v2_no_repair":
        return compile_plan_v2_no_repair(
            scene_xml, plans, standoffs_path, tracks_dir, manifest_path,
            progress_callback=progress_callback)
    if scheduler_mode == "v2":
        return compile_plan_v2(
            scene_xml, plans, standoffs_path, tracks_dir, manifest_path,
            progress_callback=progress_callback)
    if scheduler_mode != "v1":
        raise ValueError(f"unknown compiler scheduler_mode {scheduler_mode!r}")
    plan_started = time.perf_counter()
    standoffs = load_standoffs(standoffs_path)
    island = json.loads(Path(standoffs_path).read_text("utf-8"))["island_bbox"]
    manifest = json.loads(Path(manifest_path).read_text("utf-8"))
    facilities = {s["name"]: s.get("facility") for s in manifest["skills"]}
    facilities.update({name: name for name in manifest["facilities"]})
    placement_regions = placement_regions_from_manifest(manifest["facilities"])
    # close-skill-name -> its open-skill-name pair, wherever the manifest
    # derived both for a facility — lets compile_robot play a Close as the
    # exact reverse of Open when a resting object needs the ends to line up
    # exactly (see _reverse_track).
    close_to_open = {}
    reverse_always = set()  # Close skills that always play as reversed Open (drawers)
    for spec in manifest["facilities"].values():
        art = spec.get("articulation") or {}
        skills = art.get("skills") or {}
        if skills.get("open") and skills.get("close"):
            close_to_open[skills["close"]] = skills["open"]
            # slide joints (drawers) reverse cleanly; hinged doors (fridge) don't
            if any("slide" in j for j in (art.get("joints") or [])):
                reverse_always.add(skills["close"])

    # adapter: accept the nested {"tasks":[...]} authoring form or a flat
    # {robot:[steps]} form; work on a copy so caller input isn't mutated.
    plans = flatten_tasks(json.loads(json.dumps(plans)))
    # assign each place a slot within its destination facility (global across
    # robots) so multiple objects auto-distribute, not stack.
    # Sub-group by (dest, at_anchor): a place step pinned to a scene-ref anchor
    # only distributes/slots against other place steps sharing that SAME
    # anchor, not the whole facility, so multiple pins on one destination each
    # get their own local fan-out instead of being merged into one.
    per_facility = {}
    for steps in plans.values():
        for st in steps:
            if st["op"] == "place" and st.get("at") is None:
                anchor = st.get("at_anchor")
                key = (st["dest"], tuple(anchor) if anchor else None)
                per_facility.setdefault(key, []).append(st)
    for placed in per_facility.values():
        for slot, st in enumerate(placed):
            st["_slot"], st["_slot_count"] = slot, len(placed)

    ordered_steps = _container_dependency_order(plans, manifest)
    rigs = {
        robot_name: get_rig(
            scene_xml, int(robot_name.replace("robot", "")), island=island)
        for robot_name in plans
    }
    for rig in rigs.values():
        rig.set_ready(_ready_from_scene_initial(rig, manifest))
    first_rig = next(iter(rigs.values()))
    initial_q = {
        robot_name: _scene_initial_qpos(rig, manifest)
        for robot_name, rig in rigs.items()
    }
    shared_world = _make_shared_world(
        first_rig, manifest, initial_q=initial_q[next(iter(rigs))])
    contexts = {
        robot_name: _new_robot_compile_context(
            rig, initial_q=initial_q[robot_name])
        for robot_name, rig in rigs.items()
    }
    next_step = {}
    # id-of-a-step -> id of the step immediately BEFORE it in that same
    # robot's own program order (independent of compile order). D7b's
    # cause-A alias (see the D7 step-memo comment block above _STEP_MEMO)
    # can only be finalised once a step's successor has itself been
    # compiled and its completed_step is known, so this lets the successor,
    # whenever it is actually reached, find its predecessor's still-pending
    # alias registration.
    prev_step_id = {}
    for robot_steps in plans.values():
        for current, following in zip(robot_steps, robot_steps[1:]):
            next_step[current["id"]] = following
            prev_step_id[following["id"]] = current["id"]

    # D7b: per-step compile memoization keyed on actual reaching state (see
    # the block of comments/helpers above _STEP_MEMO for the full rationale
    # and the cause-A/cause-B split). `pending_alias` holds, per step id,
    # everything needed to register that step's completed-step alias once
    # its per-robot successor's own completed_step becomes known:
    # (ctx_digest_before, sw_digest_before, this step's completed-step
    # canonical form, the primary key this step's entry is actually stored
    # under). _container_dependency_order's per-robot sequencing guarantees
    # a step's predecessor is always compiled before it, so by the time a
    # step is reached here, any pending alias for ITS predecessor can always
    # be completed.
    memo_mode = _compile_memo_mode()
    memo_generation = (
        compile_memo_generation(scene_xml, standoffs_path, tracks_dir, manifest_path)
        if memo_mode != "off" else None)
    memo_hits = memo_misses = memo_uncacheable = 0
    pending_alias = {}

    def _alias_hash(ctx_digest, sw_digest, step_canon_text, next_canon_text):
        return hashlib.sha256(
            f"{memo_generation}|{ctx_digest}|{sw_digest}|{step_canon_text}|"
            f"{next_canon_text}".encode("utf-8")
        ).hexdigest()

    items = []
    # Keep request-local profiling separate from the compiled result. Memo
    # entries intentionally preserve ``compile_time_sec`` so a cache hit can
    # reproduce the original result byte-for-byte, but those values describe
    # the request that populated the cache, not this request. Only items made
    # by a live compile_robot call belong in this request's slowest/step_sum.
    compiled_now_items = []
    for robot_name, step in ordered_steps:
        op = step["op"]
        # Preserve V1's fail-before-memo semantics: an invalid shared-world
        # state must not be hidden by a cached geometry entry.
        validate_step_world_precondition(step, shared_world, manifest)
        rig = rigs[robot_name]

        memo_key = None
        ctx_digest = sw_digest = None
        step_canon = next_canon = None
        if memo_mode != "off":
            try:
                # Digest the state BEFORE this step, not the compile-order
                # path taken to reach it (D7b cause B) — see _memo_state_digest.
                ctx_digest = _memo_state_digest(contexts[robot_name])
                sw_digest = _memo_state_digest(shared_world)
                step_canon = _memo_canonical_step(step)
                nxt = next_step.get(step["id"])
                next_canon = _memo_canonical_step(nxt) if nxt is not None else "null"
            except (TypeError, ValueError):
                # Cannot canonicalise/digest confidently -> never guess a
                # key. Unlike D7's prefix hash, this affects only THIS step:
                # the state reaching every later step is still exactly known
                # (a real compile_robot call produced it, canonicalisable or
                # not), so later steps remain fully cacheable.
                memo_uncacheable += 1
            else:
                memo_key = _alias_hash(ctx_digest, sw_digest, step_canon, next_canon)

        step_started = time.perf_counter()
        cached = _step_memo_get(memo_key) if memo_key is not None else None
        resolved_primary_key = None

        if cached is not None and memo_mode == "on":
            compiled_items = cached["items"]
            # The `after` a cached entry was stored with may not be this
            # step's `after` (see _memo_canonical_step: "after" is excluded
            # from the hash on purpose) — restore it from the actual step
            # being compiled so schedule() sees the real dependency edges,
            # AND so the "completed" plan handed back to the caller (the
            # artifact the resolver/UI re-submits and edits) reports it too.
            _patch_memo_hit_after(compiled_items, step)
            contexts[robot_name] = cached["context"]
            shared_world = cached["shared_world"]
            resolved_primary_key = cached["primary_key"]
            memo_hits += 1
            _profile_log(
                f"{robot_name} step {int(step.get('_robot_order', 0)):>2} "
                f"{op:9s} memo=hit  key={memo_key[:12]}")
        else:
            compiled_items = compile_one_step(
                rig, robot_name, step,
                standoffs=standoffs,
                tracks_dir=tracks_dir,
                facilities=facilities,
                close_to_open=close_to_open,
                reverse_always=reverse_always,
                context=contexts[robot_name],
                shared_world=shared_world,
                next_step=next_step.get(step["id"]),
                placement_regions=placement_regions,
                manifest=manifest,
            )
            compiled_now_items.extend(compiled_items)
            step_elapsed = time.perf_counter() - step_started
            memo_misses += 1
            if memo_mode == "verify" and cached is not None:
                # A hit would have been served from here — prove it would
                # have been the SAME answer before anything relies on it.
                # Two fields are patched/dropped before comparing because
                # they are legitimately expected to differ without indicating
                # a wrong hit: "after" (see _memo_canonical_step — it is
                # excluded from the key by design and always overwritten from
                # the live step) and "compile_time_sec" (wall-clock
                # instrumentation, not compiled geometry — comparing it would
                # make verify mode fail on every run for reasons that have
                # nothing to do with cache soundness).
                def _for_compare(raw_items):
                    out = copy.deepcopy(raw_items)
                    _patch_memo_hit_after(out, step)
                    for item in out:
                        item.pop("compile_time_sec", None)
                    return out

                verify_items = _for_compare(compiled_items)
                cached_items = _for_compare(cached["items"])
                if not _memo_values_equal(cached_items, verify_items):
                    raise RuntimeError(
                        f"MJSKILL_COMPILE_MEMO=verify: cached items for step "
                        f"'{step['id']}' (key={memo_key[:12]}) diverge from a "
                        f"fresh compile — cache would have returned a wrong "
                        f"trajectory")
                if not _memo_values_equal(cached["context"], contexts[robot_name]):
                    raise RuntimeError(
                        f"MJSKILL_COMPILE_MEMO=verify: cached post-step "
                        f"context for '{robot_name}' after step "
                        f"'{step['id']}' (key={memo_key[:12]}) diverges from "
                        f"a fresh compile")
                if not _memo_values_equal(cached["shared_world"], shared_world):
                    raise RuntimeError(
                        f"MJSKILL_COMPILE_MEMO=verify: cached post-step "
                        f"shared_world after step '{step['id']}' "
                        f"(key={memo_key[:12]}) diverges from a fresh compile")
            if memo_key is not None:
                resolved_primary_key = _step_memo_put(
                    memo_key, compiled_items, contexts[robot_name], shared_world)
            _profile_log(
                f"{robot_name} step {int(step.get('_robot_order', 0)):>2} "
                f"{op:9s} memo={'off' if memo_mode == 'off' else 'miss'} "
                + (f"key={memo_key[:12]} " if memo_key else "uncacheable ")
                + f"compile={step_elapsed:6.2f}s")

        # D7b cause A: also index this entry under the key its own
        # completed_step would produce, so a later resubmission of the plan
        # in "completed" shape (the resolver's normal path) hits too, even
        # though completed steps carry compiler-filled fields an authored
        # step never had (see the D7 step-memo comment block for why this is
        # sound and why guessing field-by-field relevance instead is not).
        # `next` half of that alias key uses the SUCCESSOR's completed_step,
        # which isn't known yet at this point in compile order — see
        # `pending_alias` above.
        if resolved_primary_key is not None and step_canon is not None:
            step_id = step.get("id")
            cstep = compiled_items[-1].get("completed_step") if compiled_items else None
            if step_id is not None and isinstance(cstep, dict):
                try:
                    completed_canon = _memo_canonical_step(cstep)
                except (TypeError, ValueError):
                    completed_canon = None
                if completed_canon is not None:
                    if step_id in next_step:
                        pending_alias[step_id] = (
                            ctx_digest, sw_digest, completed_canon,
                            resolved_primary_key)
                    else:
                        _step_memo_add_alias(
                            _alias_hash(ctx_digest, sw_digest, completed_canon, "null"),
                            resolved_primary_key)
                    waiting_id = prev_step_id.get(step_id)
                    if waiting_id is not None:
                        info = pending_alias.pop(waiting_id, None)
                        if info is not None:
                            p_ctx, p_sw, p_completed_canon, p_primary = info
                            _step_memo_add_alias(
                                _alias_hash(p_ctx, p_sw, p_completed_canon, completed_canon),
                                p_primary)

        for item in compiled_items:
            _validate_track_robot(item)
        items += compiled_items

        apply_compiled_step_world_effect(
            step, shared_world, facilities, manifest)

    schedule(items)
    if memo_mode != "off" and (memo_hits or memo_misses or memo_uncacheable):
        considered = memo_hits + memo_misses
        rate = (memo_hits / considered * 100.0) if considered else 0.0
        _profile_log(
            f"MEMO mode={memo_mode} hits={memo_hits}/{considered} "
            f"({rate:.0f}%) uncacheable={memo_uncacheable}", level="total")
    if _COMPILE_PROFILE_MODE != "off" and items:
        total = time.perf_counter() - plan_started
        step_sum = sum(
            it.get("compile_time_sec", 0.0) for it in compiled_now_items)
        slowest = sorted(
            compiled_now_items,
            key=lambda it: it.get("compile_time_sec", 0.0),
            reverse=True)[:3]
        top = (
            ", ".join(
                f"{it['label']}={it.get('compile_time_sec', 0.0):.2f}s"
                for it in slowest)
            if slowest else "none (all steps memo hits)"
        )
        _profile_log(
            f"TOTAL compile={total:.2f}s ({len(items)} steps, "
            f"step_sum={step_sum:.2f}s) | slowest: {top}", level="total")
    # completed plan (per robot, in order): the authored steps with backend
    # spatial defaults filled in — the artifact the UI/LLM inspects and edits.
    completed = {}
    for it in items:
        completed.setdefault(it["robot"], []).append(it["completed_step"])
    conflicts = detect_conflicts(items)
    # A robot's initial physical chassis pose doubles as its rest point
    # (insert_go_to target, design doc §3c). Anonymous navigation interprets
    # explicit standoffs in this frame, not in the offset arm-mount frame.
    rest_points = {
        robot_name: [
            round(float(v), 3) for v in rig.chassis_xy(rig.model.qpos0)]
        for robot_name, rig in rigs.items()
    }
    return {
        "items": items,
        "conflicts": conflicts,
        "warnings": [c["message"] for c in conflicts],
        "completed": completed,
        "rest_points": rest_points,
    }


# ---------------------------------------------------------------- self-test
if __name__ == "__main__":
    BASE_DIR = Path(__file__).resolve().parents[3] / "frontend/public"
    SCENE = str(BASE_DIR / "assets/robocasa/layout042_study.xml")
    STUDY = BASE_DIR / "trajectories/layout042_study"
    TRACKS = STUDY / "tracks"

    args = (STUDY / "standoffs.json", TRACKS, STUDY / "skills_manifest.json")

    def show(title, plans):
        res = compile_plan(SCENE, plans, *args)
        print(f"\n=== {title} ===")
        for it in res["items"]:
            print(f"  {it['robot']} {it['label']:22s} start={it['start']:6.2f} "
                  f"dur={it['duration']:5.2f} base_xy={it['base_xy']} facility={it['facility']}")
        print("  warnings:", res["warnings"] or "none")
        return res

    # Nested authoring form (exercises the tasks->steps adapter). robot0 takes
    # mug_1 to the sink AND opens the fridge (articulation op); robot1 takes
    # mug_2 to the sink -> both contend for the sink. Detection should warn.
    conflict = {
        "tasks": [
            {"task": "mug_1 to sink", "robot": "robot0", "steps": [
                {"op": "navigate", "target": "mug_1"}, {"op": "pick", "object": "mug_1"},
                {"op": "navigate", "target": "sink"},
                {"id": "r0_place", "op": "place", "object": "mug_1", "dest": "sink"}]},
            {"task": "open fridge", "robot": "robot0", "steps": [
                {"op": "OpenFridge"}]},                       # articulation as op
            {"task": "mug_2 to sink", "robot": "robot1", "steps": [
                {"op": "navigate", "target": "mug_2"}, {"op": "pick", "object": "mug_2"},
                {"id": "r1_nav_sink", "op": "navigate", "target": "sink"},
                {"op": "place", "object": "mug_2", "dest": "sink"}]},
        ],
    }
    show("A. both -> sink + robot0 opens fridge (conflict expected)", conflict)

    # Resolution: robot1's sink approach waits for robot0 to finish placing.
    resolved = json.loads(json.dumps(conflict))
    for task in resolved["tasks"]:
        for st in task["steps"]:
            if st.get("id") == "r1_nav_sink":
                st["after"] = ["r0_place"]
    res = show("B. r1_nav_sink `after` r0_place (resolved)", resolved)

    print("\n=== completed steps (spatial defaults filled back in) ===")
    for st in res["completed"]["robot0"]:
        extra = {
            k: st[k]
            for k in ("standoff", "via_points", "route", "at")
            if k in st
        }
        print(f"  {st['id']:8s} {st['op']:10s} {extra}")

    # Keep the standalone self-test on the same generated-track isolation path
    # as the HTTP service.
    write_generated_tracks(res, TRACKS)

    # validate final: both mugs end in the sink
    rig = SceneRig(SCENE, robot=0)
    d = rig.data
    mujoco.mj_resetDataKeyframe(rig.model, d, 0)
    for it in sorted(res["items"], key=lambda x: x["start"]):
        for f in range(it["track"]["meta"]["n_frames"]):
            for jn, vals in it["track"]["channels"].items():
                a = rig.jadr(jn)
                d.qpos[a:a + len(vals[f])] = vals[f]
        mujoco.mj_forward(rig.model, d)
    sink = rig.body_xy("sink_island_group_1_main")
    for o in ("mug_1", "mug_2"):
        p = d.qpos[rig.jadr(f"{o}_joint0"):rig.jadr(f"{o}_joint0") + 3]
        print(f"{o} final", np.round(p, 3), "sink_dist_xy",
              round(float(np.linalg.norm(p[:2] - sink[:2])), 3))
