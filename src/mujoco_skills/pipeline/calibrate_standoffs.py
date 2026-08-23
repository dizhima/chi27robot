r"""Cache floor standoff base poses for the study scene's named targets.

Delegates to `skill_generators.standoff_for_point` (the single, reusable rule:
a floor position facing the target, clear of furniture footprints, within a 2D
reach band) so named targets, arbitrary user/LLM-proposed points, and
open-drawer standoffs all share one implementation. This script just runs it
over the known targets and writes `standoffs.json` as a default cache, plus a
2D top-down floor plan for eyeball review.

Run (defaults = layout042):
    uv run --with mujoco==3.10.0 --with matplotlib python -m mujoco_skills.pipeline.calibrate_standoffs

For any other scene, targets (free-joint objects + the sink) and the island
prefix are discovered from the model automatically:
    ... -m mujoco_skills.pipeline.calibrate_standoffs \
      --scene frontend/public/assets/robocasa/layout012_study.xml
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

ROBOT_BODY_PREFIXES = ("robot", "mobilebase", "gripper", "world")


def discover_targets(model, data) -> dict[str, str]:
    """name -> body for standoff targets: every free-joint (pickable) object
    plus the sink basin body."""
    targets: dict[str, str] = {}
    for j in range(model.njnt):
        if model.jnt_type[j] != mujoco.mjtJoint.mjJNT_FREE:
            continue
        body = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, int(model.jnt_bodyid[j])) or ""
        if body.startswith(ROBOT_BODY_PREFIXES):
            continue
        targets[re.sub(r"_main$", "", body)] = body
    for b in range(model.nbody):
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, b) or ""
        if name.startswith("sink") and name.endswith("_main"):
            targets["sink"] = name
            break
    return targets


def discover_island_prefix(model) -> str:
    for b in range(model.nbody):
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, b) or ""
        if name.startswith("island_") and "island_group" in name:
            return re.sub(r"_main$", "", name)
    raise ValueError("no island body found; pass --island-prefix")


def geom_xy_aabb(model, data, g):
    """Conservative XY footprint of a collision geom (rotation-aware)."""
    gtype = model.geom_type[g]
    if gtype == mujoco.mjtGeom.mjGEOM_PLANE:
        return None
    p = data.geom_xpos[g]
    if p[2] > 2.2 or p[2] < -0.1:  # ignore ceiling / below-floor
        return None
    if gtype == mujoco.mjtGeom.mjGEOM_BOX:
        s = model.geom_size[g]
        R = data.geom_xmat[g].reshape(3, 3)
        corners = np.array([
            p + R @ (np.array([sx, sy, sz]) * s)
            for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)
        ])
        return corners[:, 0].min(), corners[:, 0].max(), corners[:, 1].min(), corners[:, 1].max()
    r = float(model.geom_rbound[g])
    if r <= 0 or r > 2.5:
        return None
    return p[0] - r, p[0] + r, p[1] - r, p[1] + r


def island_bbox(model, data, island_prefix):
    # Union the accurate footprints of the island's counter-top collision geoms
    # (near tabletop height), matching what the floor plan draws.
    xs, ys = [], []
    for g in range(model.ngeom):
        bn = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, model.geom_bodyid[g]) or ""
        if not bn.startswith(island_prefix) or model.geom_group[g] != 0:
            continue
        if data.geom_xpos[g][2] > 1.05:  # only counter body / top, not tall trim
            continue
        ab = geom_xy_aabb(model, data, g)
        if ab is None:
            continue
        xs += [ab[0], ab[1]]
        ys += [ab[2], ab[3]]
    return min(xs), max(xs), min(ys), max(ys)


def main():
    from mujoco_skills.skills.skill_generators import SceneRig, standoff_for_point

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, default=DEFAULT_SCENE)
    parser.add_argument("--out-json", type=Path, default=None,
                        help="default: trajectories/<scene-stem>/standoffs.json")
    parser.add_argument("--out-png", type=Path, default=None,
                        help="default: docs/standoffs_topdown_<scene-stem>.png")
    parser.add_argument("--island-prefix", default=None)
    parser.add_argument(
        "--render-from-json",
        action="store_true",
        help="render the existing --out-json cache without recalibrating or overwriting it",
    )
    args = parser.parse_args()

    scene = args.scene
    stem = scene.stem
    out_json = args.out_json or scene.parent.parent.parent / f"trajectories/{stem}/standoffs.json"
    out_png = args.out_png or ROOT / f"docs/standoffs_topdown_{stem}.png"

    rig = SceneRig(str(scene), robot=0)
    model, data = rig.model, rig.data
    mujoco.mj_forward(model, data)
    island_prefix = args.island_prefix or discover_island_prefix(model)
    targets = discover_targets(model, data)
    print(f"island prefix: {island_prefix}; targets: {sorted(targets)}")
    bbox = island_bbox(model, data, island_prefix)
    print("island bbox x[%.3f,%.3f] y[%.3f,%.3f]" % bbox)

    rows = []
    if args.render_from_json:
        out = json.loads(out_json.read_text(encoding="utf-8"))
        for name, spec in out.get("targets", {}).items():
            body = targets.get(name)
            if body is None and name == "island":
                body = f"{island_prefix}_main"
            if body is None:
                continue
            bid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, body)
            rows.append((name, data.xpos[bid][:2].copy(), np.array(spec["standoff_xy"])))
        print("rendering existing", out_json)
    else:
        # Standoffs are cached defaults for named targets; the same
        # standoff_for_point() serves arbitrary user/LLM-proposed points.
        out = {"island_bbox": [round(v, 4) for v in bbox], "targets": {}}
        for name, body in targets.items():
            bid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, body)
            txy = data.xpos[bid][:2].copy()
            res = standoff_for_point(rig, txy, exclude_bodies={body})
            if not res["feasible"]:
                print(f"  {name:20s} INFEASIBLE: {res['reason']}")
                continue
            so = np.array(res["standoff_xy"])
            reach = float(np.linalg.norm(so - txy))
            out["targets"][name] = {
                "standoff_xy": res["standoff_xy"], "face_xy": res["face_xy"],
                "reach": round(reach, 3), "clearance": res["clearance"],
            }
            print(f"  {name:20s} standoff={np.round(so,3)} reach={reach:.2f} clear={res['clearance']:.2f}")
            rows.append((name, txy, so))

        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(out, indent=2), encoding="utf-8")
        print("wrote", out_json)

    # --- 2D top-down floor plan -----------------------------------------
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # union collision-geom footprints per body (skip robot + target objects)
    skip_prefix = ("robot", "mobilebase", "gripper", "world")
    target_bodies = set(targets.values())
    body_box = {}
    for g in range(model.ngeom):
        if model.geom_group[g] != 0:
            continue
        bn = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, model.geom_bodyid[g]) or ""
        if bn.startswith(skip_prefix) or bn in target_bodies:
            continue
        ab = geom_xy_aabb(model, data, g)
        if ab is None:
            continue
        x0, x1, y0, y1 = ab
        if bn in body_box:
            b = body_box[bn]
            body_box[bn] = (min(b[0], x0), max(b[1], x1), min(b[2], y0), max(b[3], y1))
        else:
            body_box[bn] = (x0, x1, y0, y1)

    xmin, xmax, ymin, ymax = bbox
    fig, ax = plt.subplots(figsize=(9, 9))
    for bn, (x0, x1, y0, y1) in body_box.items():
        is_wall = "wall" in bn.lower()
        ax.add_patch(plt.Rectangle(
            (x0, y0), x1 - x0, y1 - y0, fill=not is_wall,
            color="#4a4a4a" if is_wall else "#d7dad2",
            ec="#9aa0a6", lw=0.5, alpha=0.9 if is_wall else 0.55, zorder=1))
    # highlight the island the objects sit on
    ax.add_patch(plt.Rectangle((xmin, ymin), xmax - xmin, ymax - ymin,
                               fill=False, ec="#e67e22", lw=2, zorder=3, label="island"))
    for name, txy, standoff in rows:
        ax.plot(*txy, "o", color="#c0392b", zorder=5)
        ax.annotate(name, txy, fontsize=7, color="#c0392b", zorder=6,
                    xytext=(txy[0] + 0.03, txy[1] + 0.03))
        ax.plot(*standoff, "s", color="#2471a3", zorder=5)
        ax.annotate("", xy=txy, xytext=standoff, zorder=5,
                    arrowprops=dict(arrowstyle="->", color="#2471a3", lw=1.2))
    for rb, name in [("mobilebase0_base", "robot0 home"), ("mobilebase1_base", "robot1 home")]:
        bid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, rb)
        p = data.xpos[bid][:2]
        ax.plot(*p, "*", color="#27ae60", markersize=14, zorder=5)
        ax.annotate(name, p, fontsize=7, color="#27ae60", zorder=6)
    ax.set_aspect("equal"); ax.set_xlabel("x"); ax.set_ylabel("y")
    ax.set_title("floor plan: furniture (gray) + walls (dark) | island (orange) | "
                 "standoffs (blue) -> targets (red) | robot homes (green)")
    ax.grid(True, alpha=0.3)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=110, bbox_inches="tight")
    print("wrote", out_png)


if __name__ == "__main__":
    main()
