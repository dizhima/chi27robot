r"""Build the study *skills manifest* from a per-scene table + extracted tracks.

The manifest is the single source of truth the executable-plan layer, the
frontend, and the LLM orchestrator all consume. It is split into four sections
that separate *nouns* (what exists) from *verbs* (what can be done to them),
plus the narrow list of pre-recorded skills:

* **objects** — enumerable, scene-fixed things that can be picked (mug_1, ...),
  each with a world position and (if one exists) a standoff to approach it;
* **facilities** — named anchors (island / sink / fridge / counters / ...),
  each exposing whichever of `standoff` / `place` / `articulation` it actually
  supports. Capability is expressed by a field being present, not by a
  hand-typed role — a facility with `standoff: null` cannot be navigated to,
  one with `place: null` cannot be placed at, etc. This is why the recurring
  `'island'` KeyError happened before: "island" appeared in the old
  `named_facilities` table but had no matching standoff — the manifest lied
  about what was actually executable. Standoffs are now cross-referenced from
  standoffs.json, never hand-typed, so that inconsistency can't recur;
* **ops** — the parametric verbs (navigate/pick/place) and the constraints on
  their arguments. These are NOT enumerated per object/destination combo;
* **skills** — only genuine RoboCasa-replay articulation skills (Open/Close*).
  Tracks produced at runtime by compile_plan (navigate_*/pick_*/place_*, written
  back into the same tracks/ dir by write_generated_tracks) are filtered out by
  their empty `fixture_joints` — they are op instances, not skills, and
  including them previously let manifest content drift with whatever the last
  compile happened to generate.

Only a small **scene table** (`<scene>.scene_table.json`, next to the scene
XML) is hand-maintained: which facilities/objects exist, their backing bodies,
and (for facilities) place/articulation capability. Everything else --
world positions, standoffs (via cross-reference), placement regions,
precondition/effect, provenance -- is derived, so the manifest stays correct
when the scene table, tracks, or standoffs are regenerated.

Usage:
    uv run --with mujoco==3.10.0 python -m mujoco_skills.pipeline.build_skills_manifest \
      --scene       frontend/public/assets/robocasa/layout042_study.xml \
      --tracks-dir  frontend/public/trajectories/layout042_study/tracks \
      --standoffs   frontend/public/trajectories/layout042_study/standoffs.json \
      --output      frontend/public/trajectories/layout042_study/skills_manifest.json
      [--scene-table frontend/public/assets/robocasa/layout042_study.scene_table.json]
      # --scene-table defaults to <scene without .xml>.scene_table.json
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import tempfile
from pathlib import Path

import numpy as np
import mujoco

from mujoco_skills.model_signature import model_signature, robot_mounts

# Reused rather than reimplemented — real per-robot kinematics/search logic
# that only a full SceneRig provides, not a simple geometry helper like
# _geom_xy_aabb (which this module already duplicates locally):
#   - get_rig: loads/caches the SceneRig for a robot.
#   - standoff_for_point: the SAME general-purpose "find a clear, reachable,
#     facing stance" solver already used to calibrate every object/sink
#     standoff (see calibrate_standoffs.py) — reused here (A3) so a
#     container's standoff is a genuinely good place-into stance, not
#     borrowed from an unrelated use case (see the `get_rig`-adjacent note in
#     the A3 section below for why _skill_entry_standoff was the wrong tool).
from mujoco_skills.skills.skill_generators import (
    _skill_entry_standoff,
    get_rig,
    standoff_for_point,
)


# --- scene table loading -----------------------------------------------------

def _default_scene_table_path(scene_xml: Path) -> Path:
    return scene_xml.with_suffix("").with_suffix(".scene_table.json")


def load_scene_table(path: Path) -> dict:
    """Load the hand-authored scene table, preserving every section.

    Beyond the classic `facilities`/`objects`, the table may carry `pins`,
    `navigation` overrides, and per-facility `place.motion` overrides. Unknown
    top-level sections are preserved so authoring stays forward-compatible.
    """
    table = json.loads(path.read_text(encoding="utf-8"))
    table.setdefault("facilities", {})
    table.setdefault("objects", {})
    table.setdefault("pins", {})
    table.setdefault("navigation", {})
    return table


SCHEMA_VERSION = 2
GENERATOR_NAME = "mujoco_skills.pipeline.build_skills_manifest"
GENERATOR_VERSION = "2.1.0"

# Generic (scene-agnostic) container placement motion defaults, by access
# mode. Anything scene-specific — a particular fridge's insertion depth, slot
# lanes, IK tolerances — must live in that scene's scene_table under
# `facilities.<name>.place.motion`, never here.
CONTAINER_MOTION_DEFAULTS = {
    "front": {
        "release_clearance": 0.04,
        "cartesian_rrt_fallback": True,
        "front_target_offset": 0.10,
        "front_target_max_fraction": 0.70,
        "slot_spread": 0.70,
        "slot_center_frac": 0.0,
    },
    "top": {},
}

# Keys a scene table may override in place.motion (validated so a typo fails
# generation instead of silently doing nothing at compile time).
CONTAINER_MOTION_KEYS = {
    "release_clearance", "cartesian_rrt_fallback", "front_distance",
    "front_target_offset", "front_target_max_fraction", "slot_spread",
    "slot_center_frac",
    "slot_fracs", "reachin_lift_height",
    "ik_eef_tolerance", "randomize_preinsert_first", "ik_top_down",
    "rrt_horizontal_ingress", "simple_ingress",
}


def _validate_slot_points(value, json_path: str, errors: list[str]):
    """Validate and normalize fixed world-frame placement slots.

    A point may provide XY only (Z is derived from the destination support
    surface at compile time) or XYZ.  Returning ``None`` after an error keeps
    manifest generation collecting all validation failures in one pass.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        errors.append(f"{json_path}: expected a non-empty list of [x, y] or [x, y, z] points")
        return None
    normalized = []
    for index, point in enumerate(value):
        point_path = f"{json_path}[{index}]"
        if not isinstance(point, (list, tuple)) or len(point) not in (2, 3):
            errors.append(f"{point_path}: expected [x, y] or [x, y, z]")
            continue
        try:
            normalized_point = [float(component) for component in point]
        except (TypeError, ValueError):
            errors.append(f"{point_path}: coordinates must be finite numbers")
            continue
        if not all(np.isfinite(component) for component in normalized_point):
            errors.append(f"{point_path}: coordinates must be finite numbers")
            continue
        normalized.append(normalized_point)
    return normalized if len(normalized) == len(value) else None


def _validate_object_slot_points(value, json_path: str,
                                 object_names: set[str], errors: list[str]):
    """Validate object-specific world-frame placement targets."""
    if value is None:
        return None
    if not isinstance(value, dict) or not value:
        errors.append(
            f"{json_path}: expected a non-empty object-to-[x, y] or "
            "object-to-[x, y, z] mapping")
        return None
    normalized = {}
    for object_name, point in value.items():
        point_path = f"{json_path}.{object_name}"
        if object_name not in object_names:
            errors.append(f"{point_path}: unknown object '{object_name}'")
            continue
        validated = _validate_slot_points([point], point_path, errors)
        if validated is not None:
            normalized[object_name] = validated[0]
    return normalized if len(normalized) == len(value) else None

NAVIGATION_DEFAULTS = {
    "grid_resolution": 0.05,
    "obstacle_margin": 0.0,
    "nearest_free_max_distance": 0.60,
    # A replay's recorded entry base pose must be within this XY distance of
    # its facility's body — the sanity bound that would have rejected the
    # mount-retarget incident's out-of-kitchen targets.
    "replay_entry_max_facility_distance": 2.5,
}


class ManifestValidationError(Exception):
    """Raised with the full list of JSON-path-qualified validation errors."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _build_robots(model) -> dict:
    """Generated robot definitions: names validated against the model."""
    def jid(name):
        return mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)

    robots = {}
    mounts = robot_mounts(model)
    for mount_body, mount in sorted(mounts.items()):
        idx = int(mount_body.replace("robot", "").replace("_base", ""))
        arm = [f"robot{idx}_joint{i}" for i in range(1, 8)]
        fingers = [f"gripper{idx}_right_finger_joint1", f"gripper{idx}_right_finger_joint2"]
        base = [f"mobilebase{idx}_joint_mobile_forward",
                f"mobilebase{idx}_joint_mobile_side",
                f"mobilebase{idx}_joint_mobile_yaw"]
        torso = f"mobilebase{idx}_joint_torso_height"
        required = arm + fingers + base + [torso]
        missing = [n for n in required if jid(n) < 0]
        if missing:
            # not a mobile manipulator we know how to drive; record and skip
            robots[f"robot{idx}"] = {"index": idx, "unsupported_missing_joints": missing}
            continue
        f1, f2 = (model.jnt_range[jid(n)] for n in fingers)
        robots[f"robot{idx}"] = {
            "index": idx,
            "type": "pandaomron",
            "base_body": mount_body,
            "mobile_base_body": f"mobilebase{idx}_base",
            "mount": mount,
            "arm_joints": arm,
            "torso_joint": torso,
            "finger_joints": fingers,
            "base_joints": base,
            "gripper": {
                "open": [float(f1[1]), float(f2[0])],
                "closed": [0.0, 0.0],
                "ranges": [[float(f1[0]), float(f1[1])], [float(f2[0]), float(f2[1])]],
            },
            "footprint": {"base_clear": 0.35},
        }
    return robots


# --- generic body/geom geometry helpers -------------------------------------

def _body_world_pos(model, data, body_name: str) -> list[float]:
    bid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, body_name)
    if bid < 0:
        raise ValueError(f"scene table references unknown body '{body_name}'")
    return [round(float(v), 4) for v in data.xpos[bid]]


def _joint_range(model, joint_name: str) -> np.ndarray:
    jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, joint_name)
    return np.array(model.jnt_range[jid], dtype=float)


def _geom_xy_aabb(model, data, g):
    """Rotation-aware XY footprint of one collision geom (None to skip).

    A local copy of skill_generators._geom_xy_aabb's approach (box -> exact
    corners; else -> bounding-sphere radius) so this pipeline module doesn't
    reach into the skills package's internals.
    """
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


def _surface_ray_z(model, data, x: float, y: float, top: float = 1.8) -> float | None:
    """World z of the first surface hit by a ray dropped at (x, y), cast
    against the rest pose (qpos0) so nothing transient shadows the surface."""
    saved = data.qpos.copy()
    data.qpos[:] = model.qpos0
    mujoco.mj_forward(model, data)
    pnt = np.array([float(x), float(y), float(top)])
    vec = np.array([0.0, 0.0, -1.0])
    gid = np.zeros(1, dtype=np.int32)
    dist = mujoco.mj_ray(model, data, pnt, vec, None, 1, -1, gid)
    data.qpos[:] = saved
    mujoco.mj_forward(model, data)
    if gid[0] < 0 or dist < 0:
        return None
    return float(top - dist)


def _compute_place_region(model, data, surface_body: str, override: dict | None) -> dict | None:
    """A place region for `surface_body`: an override (hand-tuned basin/shelf
    area) if given, else the union XY footprint of the body's own group-0
    (collision) geoms, with z from a downward raycast at the region center.

    The generated region is consumed directly by
    ``skill_generators.placement_regions_from_manifest`` and ``dest_point``.
    """
    bxy = _body_world_pos(model, data, surface_body)
    if override is not None:
        half = override.get("half") or override.get("inset")
        z_offset = override.get("z_offset", 0.0)
        return {
            "center_xy": [round(bxy[0], 3), round(bxy[1], 3)],
            "z": round(bxy[2] + z_offset, 3),
            "half": [round(float(half[0]), 3), round(float(half[1]), 3)],
            "source": "override",
        }

    bid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, surface_body)
    xs: list[float] = []
    ys: list[float] = []
    for g in range(model.body_geomadr[bid], model.body_geomadr[bid] + model.body_geomnum[bid]):
        if model.geom_group[g] != 0:
            continue
        ab = _geom_xy_aabb(model, data, g)
        if ab:
            xs += [ab[0], ab[1]]
            ys += [ab[2], ab[3]]
    if not xs:
        return None
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    half = [(max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2]
    z = _surface_ray_z(model, data, cx, cy)
    if z is None:
        z = bxy[2]
    return {
        "center_xy": [round(cx, 3), round(cy, 3)],
        "z": round(z, 3),
        "half": [round(half[0], 3), round(half[1], 3)],
        "source": "computed",
    }


def _classify_state(value: float, jrange: np.ndarray) -> str:
    """closed ≈ 0, open = any meaningful displacement toward the range extreme.

    A demo may start only partially open (e.g. the CloseDrawer demo begins at
    ~50% travel), so a nearest-endpoint rule mis-labels it. Instead treat the
    joint as closed only when it is within 15% of full travel of zero.
    """
    lo, hi = float(jrange[0]), float(jrange[1])
    open_extreme = lo if abs(lo) > abs(hi) else hi
    closed_band = 0.15 * abs(open_extreme)
    return "closed" if abs(value) <= closed_band else "open"


# --- standoffs cross-reference (never hand-typed) ---------------------------

def _load_standoff_targets(standoffs_path: Path) -> dict[str, dict]:
    data = json.loads(standoffs_path.read_text(encoding="utf-8"))
    out = {}
    for name, v in data.get("targets", {}).items():
        out[name] = {
            "standoff_xy": [round(float(v["standoff_xy"][0]), 3), round(float(v["standoff_xy"][1]), 3)],
            "face_xy": [round(float(v["face_xy"][0]), 3), round(float(v["face_xy"][1]), 3)],
        }
    return out


# --- ops: parametric verbs, declared once, never enumerated per instance ----

OPS = {
    "navigate": {
        "generated": True,
        "params": {"target": "an object or facility name whose manifest entry has a non-null `standoff`"},
    },
    "pick": {
        "generated": True,
        "params": {
            "object": "an object name from `objects` (all listed objects are pickable)",
            "grasp_mode": "optional: 'top_down' (default) or 'horizontal'",
            "return_to_ready": "horizontal pick only; optional boolean, defaults to true",
        },
    },
    "place": {
        "generated": True,
        "params": {
            "object": "the currently-held object",
            "dest": "a facility name whose manifest entry has a non-null `place`",
        },
    },
    "reset": {
        "generated": True,
        "params": {
            "retreat": "optional backward distance in metres (default 0.18)",
            "preserve_yaw": "must be true; retreat without turning",
        },
    },
}


def build_manifest(scene_xml: Path, tracks_dir: Path, scene_table_path: Path,
                    standoffs_path: Path) -> dict:
    scene_table = load_scene_table(scene_table_path)
    model = mujoco.MjModel.from_xml_path(str(scene_xml))
    data = mujoco.MjData(model)

    init_keyframe = None
    for k in range(model.nkey):
        init_keyframe = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_KEY, k)
        break  # convention: first keyframe is the session init state

    # Derive world positions and initial semantic states from the SESSION INIT
    # state, not qpos0 — a baked keyframe may hold fixtures open or arms posed.
    if init_keyframe is not None:
        mujoco.mj_resetDataKeyframe(model, data, 0)
    mujoco.mj_forward(model, data)

    validation_errors: list[str] = []

    def _require_body(json_path: str, body: str):
        if mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, body) < 0:
            validation_errors.append(f"{json_path}: unknown body '{body}'")

    def _require_joint(json_path: str, joint: str):
        if mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, joint) < 0:
            validation_errors.append(f"{json_path}: unknown joint '{joint}'")

    standoff_targets = _load_standoff_targets(standoffs_path)

    # facility name a fixture joint belongs to, for skill->facility reverse lookup
    joint_to_fac: dict[str, str] = {}
    for fac, spec in scene_table["facilities"].items():
        art = spec.get("articulation")
        if art:
            for jn in art["joints"]:
                joint_to_fac[jn] = fac

    # --- gather tracks: {skill: {robot: track_dict}} -------------------------
    # Canonical RoboCasa tracks live directly in tracks/robot*/. Compile-time
    # artifacts are written to tracks/_generated/robot*/ and are deliberately
    # outside this scan: an augmented Close*_with_<object> track can contain a
    # free-joint object channel and must never become an LLM-selectable skill.
    skills: dict[str, dict[str, dict]] = {}
    skipped_generated = []
    for robot_dir in sorted(
            p for p in tracks_dir.iterdir()
            if p.is_dir() and p.name.startswith("robot")):
        robot = robot_dir.name
        for track_path in sorted(robot_dir.glob("*.track.json")):
            track = json.loads(track_path.read_text(encoding="utf-8"))
            skill = track["meta"]["skill"]
            if not track["meta"].get("fixture_joints"):
                skipped_generated.append(f"{robot}/{skill}")
                continue
            skills.setdefault(skill, {})[robot] = {
                "track": str(track_path.relative_to(tracks_dir.parent)).replace("\\", "/"),
                "data": track,
            }

    skill_entries = []
    for skill in sorted(skills):
        per_robot = skills[skill]
        any_track = next(iter(per_robot.values()))["data"]
        recorded_fixture_joints = any_track["meta"]["fixture_joints"]

        facilities_hit = sorted({joint_to_fac[j] for j in recorded_fixture_joints if j in joint_to_fac})
        facility = facilities_hit[0] if len(facilities_hit) == 1 else (facilities_hit or None)

        # The scene table is authoritative about which joints define a
        # facility's semantic state.  Dataset demonstrations can contain small
        # incidental motion in a neighbouring door (for example OpenFridge
        # ep64 moves the freezer door by ~1 degree before opening the fridge
        # door by ~90 degrees).  Do not let that unlisted drift determine a
        # skill's precondition/effect or leak into the executable joint set.
        fixture_joints = recorded_fixture_joints
        if isinstance(facility, str):
            semantic_joints = [
                j for j in recorded_fixture_joints
                if joint_to_fac.get(j) == facility
            ]
            if semantic_joints:
                fixture_joints = semantic_joints

        precondition = effect = None
        if fixture_joints:
            j = fixture_joints[0]
            chan = any_track["channels"][j]
            start_v, end_v = float(chan[0][0]), float(chan[-1][0])
            jr = _joint_range(model, j)
            precondition = _classify_state(start_v, jr)
            effect = _classify_state(end_v, jr)

        tracks_by_robot = {r: v["track"] for r, v in per_robot.items()}
        retargeted = any_track["meta"].get("retargeted_from")
        reversed_from = any_track["meta"].get("reversed_from")
        if reversed_from:
            implementation = "reverse_replay"
        elif retargeted:
            implementation = "retargeted_replay"
        else:
            implementation = "fixed_replay"

        # per-robot recording provenance and entry/exit poses, read from each
        # track's own metadata (never a global table)
        recording_by_robot = {}
        entry_by_robot = {}
        exit_by_robot = {}
        for r, v in per_robot.items():
            meta = v["data"]["meta"]
            rec = meta.get("recording")
            if not rec or not rec.get("robot_mount"):
                validation_errors.append(
                    f"skills.{skill}.recording.{r}: track '{v['track']}' has no "
                    "meta.recording.robot_mount (re-extract or run "
                    "migrate_track_mounts)")
            else:
                recording_by_robot[r] = rec
            entry_by_robot[r] = meta.get("entry_base_pose")
            exit_by_robot[r] = meta.get("exit_base_pose")

        for jn in fixture_joints:
            _require_joint(f"skills.{skill}.fixture_joints", jn)

        skill_entries.append(
            {
                "name": skill,
                "type": "articulation",
                "implementation": implementation,
                "facility": facility,
                "fixture_joints": fixture_joints,
                "precondition": {"facility_state": precondition} if precondition else {},
                "effect": {"facility_state": effect} if effect else {},
                "robots": sorted(per_robot.keys()),
                "tracks": tracks_by_robot,
                "duration": round(float(any_track["meta"]["duration"]), 3),
                "n_frames": int(any_track["meta"]["n_frames"]),
                "provenance": "retargeted" if retargeted else ("reversed" if reversed_from else "dataset"),
                "retargeted_from": retargeted,
                "reversed_from": reversed_from,
                "recording": recording_by_robot,
                "entry_base_pose": entry_by_robot,
                "exit_base_pose": exit_by_robot,
            }
        )

    # A3: a container's own standoff is missing from standoffs.json (nothing
    # ever calibrated a generic approach point for one) — backfill it from its
    # `requires_open` skill's own recorded entry pose, exactly like
    # compile_robot already does when a navigate immediately precedes a
    # pre-recorded articulation skill.
    #
    # This must land in BOTH standoff_targets (so the manifest — read below —
    # reports can_navigate truthfully) AND back on disk in standoffs.json,
    # because compile_robot's navigate branch loads that file completely
    # independently of this manifest (`load_standoffs(standoffs_path)` inside
    # skill_generators.compile_plan) — a container's standoff living only in
    # the manifest would leave `standoffs[tgt]` KeyError-ing at compile time
    # exactly like the old "island" bug this cross-reference design was meant
    # to prevent, just moved to a new name.
    #
    # Computed via standoff_for_point — the SAME general clear/reachable/
    # facing-stance solver already used to calibrate every object/sink
    # standoff — NOT via the Open/Close skill's own recorded entry pose
    # (_skill_entry_standoff). That reuse was a mistake in the first version
    # of this backfill: the recorded demo positions the robot to reach the
    # drawer's HANDLE to operate the slide, which needn't be square-on to the
    # drawer (observed ~0.57m off-center) — fine for "navigate right before
    # replaying that skill" (compile_robot's separate, still-correct
    # entry-pose logic for that case), wrong for "stand somewhere good to
    # manually reach in and place something", which is what THIS standoff is
    # actually used for. standoff_for_point's clearance search, run against
    # the container's OPEN footprint (working_q below), naturally converges
    # on the one side that isn't boxed in by neighboring furniture — for a
    # drawer embedded in a counter run, that's the front.
    newly_backfilled: dict[str, dict] = {}
    for name, spec in scene_table["facilities"].items():
        if name in standoff_targets:
            continue
        place_cfg = spec.get("place")
        if not place_cfg or place_cfg.get("kind") != "container":
            continue
        requires_open = place_cfg.get("requires_open")
        open_skill = next((s for s in skill_entries if s["name"] == requires_open), None)
        if open_skill is None and requires_open is None:
            # upper_cabinet is already open in study_init and has no OpenCabinet
            # replay. Its CloseCabinet entry frame is, by definition, the
            # recorded open state and a proven stance from which the arm reaches
            # the cabinet. Use that entry pose only for this default-open case;
            # drawers keep the square-on standoff_for_point(open:...) path below.
            close_skill = next(
                (s for s in skill_entries
                 if s["facility"] == name
                 and s["effect"].get("facility_state") == "closed"),
                None,
            )
            if close_skill is None:
                continue
            rig = get_rig(str(scene_xml), 0)
            track_rel = (
                close_skill["tracks"].get("robot0")
                or next(iter(close_skill["tracks"].values()))
            )
            track = json.loads((tracks_dir.parent / track_rel).read_text(encoding="utf-8"))
            entry = _skill_entry_standoff(rig, track)
            entry_xy = np.asarray(entry["standoff_xy"], dtype=float)
            face_xy = np.asarray(entry["face_xy"], dtype=float)
            # CloseCabinet's entry is a demonstrated, collision-free
            # cabinet-facing stance. Reuse it exactly: advancing the mobile
            # base toward the cabinet can overlap the lower cabinet run.
            place_xy = entry_xy
            so = {
                "standoff_xy": [
                    round(float(v), 3) for v in place_xy
                ],
                "face_xy": [
                    round(float(v), 3) for v in face_xy
                ],
            }
            standoff_targets[name] = so
            newly_backfilled[name] = {
                **so,
                "derived_from": (
                    f"_skill_entry_standoff({close_skill['name']})"
                ),
            }
            continue
        if open_skill is None:
            continue  # requires_open names a skill that isn't in this scene's tracks
        rig = get_rig(str(scene_xml), 0)

        # working_q: qpos0 with the container's own fixture joint(s) driven to
        # this Open skill's last-frame (i.e. actually-open, however far the
        # real demo opens it) value — the SAME reference state
        # _container_dest_point uses at compile time, so the standoff and the
        # placement point agree on what "open" means for this container.
        track_rel = open_skill["tracks"].get("robot0") or next(iter(open_skill["tracks"].values()))
        track = json.loads((tracks_dir.parent / track_rel).read_text(encoding="utf-8"))
        working_q = model.qpos0.copy()
        for jn in open_skill["fixture_joints"]:
            working_q[rig.jadr(jn)] = float(track["channels"][jn][-1][0])

        rig.data.qpos[:] = working_q
        mujoco.mj_forward(rig.model, rig.data)
        target_xy = rig.body_xy(place_cfg["interior_body"])[:2].copy()

        result = standoff_for_point(rig, target_xy, working_q=working_q)
        if not result["feasible"]:
            # A French-door fridge has no collision-free *radial* sample around
            # its deep interior center when both doors are open: the two door
            # leaves occupy the side sectors and the counter blocks the rear.
            # Its OpenFridge replay entry is nevertheless a real, proven front
            # stance. Only use this fallback when the general solver has no
            # answer; drawers/cabinets still prefer the independent square-on
            # standoff_for_point result whenever one exists.
            entry = _skill_entry_standoff(rig, track)
            so = {
                "standoff_xy": [round(float(v), 3) for v in entry["standoff_xy"]],
                "face_xy": [round(float(v), 3) for v in entry["face_xy"]],
            }
            derived_from = f"_skill_entry_standoff({requires_open}) (open-footprint fallback)"
        else:
            so = {
                "standoff_xy": result["standoff_xy"],
                "face_xy": result["face_xy"],
            }
            derived_from = f"standoff_for_point(open:{requires_open})"
        standoff_targets[name] = so
        newly_backfilled[name] = {**so, "derived_from": derived_from}

    if newly_backfilled:
        raw = json.loads(standoffs_path.read_text(encoding="utf-8"))
        raw.setdefault("targets", {})
        raw["targets"].update(newly_backfilled)
        standoffs_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    # --- objects --------------------------------------------------------------
    objects_out = {}
    for name, spec in scene_table["objects"].items():
        _require_body(f"objects.{name}.body", spec["body"])
        known = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, spec["body"]) >= 0
        pick_cfg = spec.get("pick")
        normalized_pick = None
        if pick_cfg is not None:
            if not isinstance(pick_cfg, dict):
                validation_errors.append(
                    f"objects.{name}.pick: expected an object")
            else:
                grasp_mode = pick_cfg.get("grasp_mode", "top_down")
                if grasp_mode not in {"top_down", "horizontal"}:
                    validation_errors.append(
                        f"objects.{name}.pick.grasp_mode: expected "
                        "'top_down' or 'horizontal'")
                return_to_ready = pick_cfg.get("return_to_ready", True)
                if not isinstance(return_to_ready, bool):
                    validation_errors.append(
                        f"objects.{name}.pick.return_to_ready: expected boolean")
                grasp_offset = pick_cfg.get("grasp_offset")
                normalized_grasp_offset = None
                if grasp_offset is not None:
                    offset_path = f"objects.{name}.pick.grasp_offset"
                    if (not isinstance(grasp_offset, (list, tuple))
                            or len(grasp_offset) != 3):
                        validation_errors.append(
                            f"{offset_path}: expected [x, y, z]")
                    else:
                        try:
                            candidate_offset = [
                                float(component) for component in grasp_offset]
                        except (TypeError, ValueError):
                            candidate_offset = None
                        if (candidate_offset is None
                                or not all(np.isfinite(candidate_offset))):
                            validation_errors.append(
                                f"{offset_path}: coordinates must be finite numbers")
                        else:
                            normalized_grasp_offset = candidate_offset
                if (grasp_mode in {"top_down", "horizontal"}
                        and isinstance(return_to_ready, bool)):
                    normalized_pick = {"grasp_mode": grasp_mode}
                    if grasp_mode == "horizontal":
                        normalized_pick["return_to_ready"] = return_to_ready
                    if normalized_grasp_offset is not None:
                        normalized_pick["grasp_offset"] = normalized_grasp_offset
        objects_out[name] = {
            "label": spec.get("label", name),
            "body": spec["body"],
            "show_scene_label": spec.get("show_scene_label", True),
            "home_facility": spec.get("home_facility"),
            "world_pos": _body_world_pos(model, data, spec["body"]) if known else None,
            "pickable": bool(spec.get("pickable", True)),
            "standoff": standoff_targets.get(name),  # None if not (yet) generated
            **({"pick": normalized_pick} if normalized_pick is not None else {}),
        }

    # --- facilities -------------------------------------------------------------
    facilities_out = {}
    for name, spec in scene_table["facilities"].items():
        place = None
        place_cfg = spec.get("place")
        if place_cfg is not None:
            kind = place_cfg.get("kind", "surface")
            slot_points = _validate_slot_points(
                place_cfg.get("slot_points"),
                f"facilities.{name}.place.slot_points",
                validation_errors,
            )
            object_slot_points = _validate_object_slot_points(
                place_cfg.get("object_slot_points"),
                f"facilities.{name}.place.object_slot_points",
                set(scene_table["objects"]),
                validation_errors,
            )
            if kind == "container":
                # A container's interior has physically moved from its rest
                # pose once open (e.g. a drawer slides ~0.6m) — a build-time
                # region computed at qpos0 would describe the CLOSED position,
                # silently wrong. So unlike the surface case, no region is
                # precomputed here; skill_generators.dest_point computes the
                # actual point at compile time, against the currently-open
                # state (see docs/container_placement_design.md Phase A / A2).
                access = place_cfg.get("access", "top")
                requires_open = place_cfg.get("requires_open")
                overrides = dict(place_cfg.get("motion") or {})
                unknown = sorted(set(overrides) - CONTAINER_MOTION_KEYS)
                if unknown:
                    validation_errors.append(
                        f"facilities.{name}.place.motion: unknown motion "
                        f"override key(s) {unknown}")
                motion = {**CONTAINER_MOTION_DEFAULTS.get(access, {}), **overrides}
                place = {
                    "kind": "container",
                    "access": access,
                    "interior_body": place_cfg["interior_body"],
                    "support_geom": place_cfg.get("support_geom"),
                    "requires_open": requires_open,
                    # which qpos to evaluate the container geometry against at
                    # compile time: after its Open skill ("current") or the
                    # session-init keyframe (already-open fixtures)
                    "state_source": place_cfg.get(
                        "state_source",
                        "current" if requires_open else "study_init"),
                    **({"slot_points": slot_points} if slot_points is not None else {}),
                    **({"object_slot_points": object_slot_points}
                       if object_slot_points is not None else {}),
                    **motion,
                    "provenance": {
                        "motion_overrides": sorted(overrides),
                        "motion_defaults": sorted(
                            set(CONTAINER_MOTION_DEFAULTS.get(access, {})) - set(overrides)),
                    },
                }
                _require_body(f"facilities.{name}.place.interior_body",
                              place_cfg["interior_body"])
                if place_cfg.get("support_geom") is not None and mujoco.mj_name2id(
                        model, mujoco.mjtObj.mjOBJ_GEOM, place_cfg["support_geom"]) < 0:
                    validation_errors.append(
                        f"facilities.{name}.place.support_geom: unknown geom "
                        f"'{place_cfg['support_geom']}'")
            else:
                region = _compute_place_region(model, data, place_cfg["surface_body"],
                                               place_cfg.get("override"))
                place = {
                    "surface_body": place_cfg["surface_body"],
                    "region": region,
                    **({"slot_points": slot_points} if slot_points is not None else {}),
                    **({"object_slot_points": object_slot_points}
                       if object_slot_points is not None else {}),
                }
                _require_body(f"facilities.{name}.place.surface_body",
                              place_cfg["surface_body"])

        _require_body(f"facilities.{name}.body", spec["body"])

        articulation = None
        art_cfg = spec.get("articulation")
        if art_cfg is not None:
            for jn in art_cfg["joints"]:
                _require_joint(f"facilities.{name}.articulation.joints", jn)
            skill_map = {}
            for s in skill_entries:
                if s["facility"] != name:
                    continue
                eff = s["effect"].get("facility_state")
                if eff == "open":
                    skill_map["open"] = s["name"]
                elif eff == "closed":
                    skill_map["close"] = s["name"]
            articulation = {
                "joints": art_cfg["joints"],
                "initial_state": art_cfg["initial_state"],
                "skills": skill_map,
            }

        known = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, spec["body"]) >= 0
        facilities_out[name] = {
            "label": spec.get("label", name),
            "body": spec["body"],
            "show_scene_label": spec.get("show_scene_label", True),
            **({"scene_label_xy": spec["scene_label_xy"]}
               if spec.get("scene_label_xy") is not None else {}),
            **({"scene_label_z": spec["scene_label_z"]}
               if spec.get("scene_label_z") is not None else {}),
            "world_pos": _body_world_pos(model, data, spec["body"]) if known else None,
            "standoff": standoff_targets.get(name),  # includes A3 container backfill above
            "place": place,
            "articulation": articulation,
        }

    # --- replay entry vs facility sanity check --------------------------------
    navigation = {**NAVIGATION_DEFAULTS, **scene_table.get("navigation", {})}
    max_entry_dist = float(navigation["replay_entry_max_facility_distance"])
    for s in skill_entries:
        fac = facilities_out.get(s["facility"]) if isinstance(s["facility"], str) else None
        if not fac or not fac.get("world_pos"):
            continue
        fxy = np.asarray(fac["world_pos"][:2], dtype=float)
        for r, pose in (s.get("entry_base_pose") or {}).items():
            if not pose:
                validation_errors.append(
                    f"skills.{s['name']}.entry_base_pose.{r}: missing (re-extract track)")
                continue
            dist = float(np.linalg.norm(np.asarray(pose["xy"]) - fxy))
            if dist > max_entry_dist:
                validation_errors.append(
                    f"skills.{s['name']}.entry_base_pose.{r}: entry {pose['xy']} is "
                    f"{dist:.2f}m from facility '{s['facility']}' "
                    f"(> {max_entry_dist}m) — wrong recorded mount or wrong scene")

    # --- state model -----------------------------------------------------------
    state_model = {}
    for name, fac in facilities_out.items():
        art = fac.get("articulation")
        if not art:
            continue
        transitions = []
        for s in skill_entries:
            if s["facility"] != name:
                continue
            pre = s["precondition"].get("facility_state")
            eff = s["effect"].get("facility_state")
            if pre and eff:
                transitions.append({"skill": s["name"], "from": pre, "to": eff})
        state_model[name] = {
            "states": ["open", "closed"],
            "initial": art["initial_state"],
            "transitions": transitions,
            "observable_joints": art["joints"],
        }

    # --- pins (fixture-local; execution support is validated by the compiler) --
    pins_out = {}
    for pin_id, pin in scene_table.get("pins", {}).items():
        owner = pin.get("facility")
        if owner not in facilities_out:
            validation_errors.append(f"pins.{pin_id}.facility: unknown facility '{owner}'")
            continue
        if pin.get("frame_body"):
            _require_body(f"pins.{pin_id}.frame_body", pin["frame_body"])
        pins_out[pin_id] = {
            "label": pin.get("label", pin_id),
            "facility": owner,
            "frame_body": pin.get("frame_body"),
            "local_pos": pin.get("local_pos"),
            "clearance": pin.get("clearance"),
            "allowed_objects": pin.get("allowed_objects"),
        }

    if validation_errors:
        raise ManifestValidationError(validation_errors)

    # Normalize separators BEFORE splitting on "public/" — on Windows str(Path)
    # uses backslashes, so splitting on a literal forward slash first (as the
    # previous version of this function did) silently never matched and left
    # the full absolute/relative path in the manifest.
    xml_norm = str(scene_xml).replace("\\", "/")
    xml_rel = xml_norm.split("public/")[-1] if "public/" in xml_norm else scene_xml.name
    mjb_path = scene_xml.with_suffix(".mjb")

    return {
        "schema_version": SCHEMA_VERSION,
        "scene": {
            "id": scene_xml.stem,
            "xml": xml_rel,
            "mjb": xml_rel.replace(".xml", ".mjb") if mjb_path.exists() else None,
            "xml_sha256": _sha256_file(scene_xml),
            "model_signature": model_signature(model),
            "mujoco_version": mujoco.__version__,
            "nq": int(model.nq),
            "nv": int(model.nv),
            "init_keyframe": init_keyframe,
            "coordinate_frame": "world",
            "units": {"length": "meter", "angle": "radian"},
        },
        "robots": _build_robots(model),
        "objects": objects_out,
        "facilities": facilities_out,
        "ops": OPS,
        "skills": skill_entries,
        "navigation": navigation,
        "pins": pins_out,
        "state_model": state_model,
        "validation": {
            "checks": [
                "all referenced bodies/joints/geoms exist in the compiled model",
                "every fixed track carries meta.recording.robot_mount",
                f"replay entry within {max_entry_dist}m of its facility",
                "container motion overrides restricted to known keys",
            ],
            "errors": [],
        },
        "generated": {
            "generator": GENERATOR_NAME,
            "generator_version": GENERATOR_VERSION,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "scene_table": scene_table_path.name,
            "overrides": sorted(
                f"facilities.{n}.place.motion.{k}"
                for n, spec in scene_table["facilities"].items()
                for k in ((spec.get("place") or {}).get("motion") or {})
            ),
        },
        "notes": {
            "playback": "Tracks are name-addressed channel tracks; reset to "
            "scene.init_keyframe, then apply each skill's channels for the assigned robot.",
            "excluded_generated_tracks": skipped_generated,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--tracks-dir", type=Path, required=True)
    parser.add_argument("--standoffs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scene-table", type=Path, default=None,
                        help="defaults to <scene without .xml>.scene_table.json")
    args = parser.parse_args()

    scene_table_path = args.scene_table or _default_scene_table_path(args.scene)
    try:
        manifest = build_manifest(args.scene, args.tracks_dir, scene_table_path, args.standoffs)
    except ManifestValidationError as error:
        raise SystemExit(
            "manifest generation FAILED validation:\n  " + "\n  ".join(error.errors)
        ) from error

    # atomic write: serialize the complete validated manifest, then replace
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(args.output.parent), prefix=args.output.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, args.output)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    print(f"wrote {args.output} (schema_version={manifest['schema_version']})")
    print(f"  scene nq={manifest['scene']['nq']} init_keyframe={manifest['scene']['init_keyframe']}")
    print(f"  objects: {list(manifest['objects'].keys())}")
    print(f"  facilities: {list(manifest['facilities'].keys())}")
    if manifest["notes"]["excluded_generated_tracks"]:
        print(f"  excluded {len(manifest['notes']['excluded_generated_tracks'])} generated "
              f"(non-skill) tracks: {manifest['notes']['excluded_generated_tracks']}")
    for s in manifest["skills"]:
        pc = s["precondition"].get("facility_state", "-")
        ef = s["effect"].get("facility_state", "-")
        print(
            f"  {s['name']:20s} facility={s['facility']} "
            f"{pc}->{ef} robots={s['robots']} [{s['provenance']}]"
        )


if __name__ == "__main__":
    main()
