r"""Build a reusable static 2D navigation map for any MuJoCo scene.

The map is a conservative configuration-space approximation for a mobile base:
floor support projected into XY, minus world-fixed collision geometry inflated
by the base radius plus margin. Robots, free objects, and articulated bodies are
excluded from this *static* layer; they belong in later dynamic layers.

By default all artifacts are written beside the input scene:

    <scene>.navgrid.npz       machine-readable masks and coordinates
    <scene>.navgrid.json      metadata / format contract
    <scene>.floorplan.png     human-review rendering

Run:
    uv run --with matplotlib python -m \
      mujoco_skills.pipeline.build_navigation_grid path/to/scene.xml
"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import mujoco
import numpy as np


FLOOR_NAME_TOKENS = ("floor", "ground")
_GRID_CACHE: dict[Path, tuple[int, "NavigationGrid"]] = {}
_GRID_CACHE_LOCK = threading.Lock()


@dataclass
class NavigationGrid:
    scene: Path
    resolution: float
    robot_radius: float
    margin: float
    bounds: tuple[float, float, float, float]
    x: np.ndarray
    y: np.ndarray
    floor_mask: np.ndarray
    raw_obstacle_mask: np.ndarray
    navigable: np.ndarray
    floor_geom_names: list[str]
    obstacle_geom_names: list[str]
    bounds_source: str

    @property
    def occupied(self) -> np.ndarray:
        return ~self.navigable

    def cell_for_xy(self, xy) -> tuple[int, int] | None:
        xmin, xmax, ymin, ymax = self.bounds
        px, py = float(xy[0]), float(xy[1])
        if not (xmin <= px < xmax and ymin <= py < ymax):
            return None
        col = int((px - xmin) / self.resolution)
        row = int((py - ymin) / self.resolution)
        if row >= self.navigable.shape[0] or col >= self.navigable.shape[1]:
            return None
        return row, col

    def is_navigable_xy(self, xy) -> bool:
        cell = self.cell_for_xy(xy)
        return bool(cell is not None and self.navigable[cell])


def _object_name(model, kind, index) -> str:
    return mujoco.mj_id2name(model, kind, int(index)) or ""


def geom_xy_aabb(model, data, geom_id):
    """Conservative rotation-aware world-XY footprint of one finite geom."""
    geom_type = model.geom_type[geom_id]
    if geom_type == mujoco.mjtGeom.mjGEOM_PLANE:
        return None
    position = data.geom_xpos[geom_id]
    if geom_type == mujoco.mjtGeom.mjGEOM_BOX:
        size = model.geom_size[geom_id]
        rotation = data.geom_xmat[geom_id].reshape(3, 3)
        corners = np.array([
            position + rotation @ (np.array([sx, sy, sz]) * size)
            for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)
        ])
        return (
            float(corners[:, 0].min()), float(corners[:, 0].max()),
            float(corners[:, 1].min()), float(corners[:, 1].max()),
        )
    radius = float(model.geom_rbound[geom_id])
    if radius <= 0 or not np.isfinite(radius):
        return None
    return (
        float(position[0] - radius), float(position[0] + radius),
        float(position[1] - radius), float(position[1] + radius),
    )


def _geom_z_bounds(model, data, geom_id) -> tuple[float, float]:
    position = data.geom_xpos[geom_id]
    geom_type = model.geom_type[geom_id]
    if geom_type == mujoco.mjtGeom.mjGEOM_PLANE:
        return float(position[2]), float(position[2])
    if geom_type == mujoco.mjtGeom.mjGEOM_BOX:
        # geom_rbound is a 3-D bounding-sphere radius.  Using it here makes a
        # wide, thin floor appear several metres tall, which in turn shifts the
        # navigation height band above ordinary furniture.  Project the box's
        # oriented half-extents onto world Z instead.
        rotation = data.geom_xmat[geom_id].reshape(3, 3)
        half_height = float(
            np.sum(np.abs(rotation[2, :]) * model.geom_size[geom_id]))
        return float(position[2] - half_height), float(position[2] + half_height)
    radius = float(model.geom_rbound[geom_id])
    if not np.isfinite(radius) or radius <= 0:
        radius = float(np.max(model.geom_size[geom_id]))
    return float(position[2] - radius), float(position[2] + radius)


def _body_is_world_fixed(model, body_id: int) -> bool:
    """True when this body and every ancestor have no joint/DOF."""
    current = int(body_id)
    while current > 0:
        if int(model.body_jntnum[current]) > 0:
            return False
        current = int(model.body_parentid[current])
    return True


def _geom_label(model, geom_id: int) -> str:
    geom_name = _object_name(model, mujoco.mjtObj.mjOBJ_GEOM, geom_id)
    body_name = _object_name(
        model, mujoco.mjtObj.mjOBJ_BODY, model.geom_bodyid[geom_id])
    return f"{geom_name} {body_name}".strip().lower()


def _horizontal_box(model, data, geom_id: int) -> bool:
    if model.geom_type[geom_id] != mujoco.mjtGeom.mjGEOM_BOX:
        return False
    rotation = data.geom_xmat[geom_id].reshape(3, 3)
    return abs(float(rotation[2, 2])) >= 0.95


def _find_floor_geoms(model, data) -> list[int]:
    named = []
    for geom_id in range(model.ngeom):
        geom_type = model.geom_type[geom_id]
        label = _geom_label(model, geom_id)
        if geom_type == mujoco.mjtGeom.mjGEOM_PLANE:
            named.append(geom_id)
        elif (_horizontal_box(model, data, geom_id)
              and any(token in label for token in FLOOR_NAME_TOKENS)):
            named.append(geom_id)
    if named:
        return named

    # Naming-free fallback: large, low, horizontal, world-fixed boxes. This is
    # intentionally conservative and only used when a scene exposes no obvious
    # floor/ground geom names or plane.
    candidates = []
    for geom_id in range(model.ngeom):
        if not _horizontal_box(model, data, geom_id):
            continue
        if not _body_is_world_fixed(model, model.geom_bodyid[geom_id]):
            continue
        aabb = geom_xy_aabb(model, data, geom_id)
        if aabb is None:
            continue
        area = (aabb[1] - aabb[0]) * (aabb[3] - aabb[2])
        top = float(data.geom_xpos[geom_id][2] + model.geom_size[geom_id][2])
        if area >= 1.0:
            candidates.append((top, -area, geom_id))
    if not candidates:
        raise ValueError(
            "could not identify a floor geom; name it with 'floor'/'ground', "
            "use a plane geom, or pass explicit --bounds")
    lowest_top = min(item[0] for item in candidates)
    return [geom_id for top, _neg_area, geom_id in candidates
            if top <= lowest_top + 0.08]


def _infer_bounds(model, data, floor_geoms, explicit_bounds=None):
    if explicit_bounds is not None:
        xmin, xmax, ymin, ymax = map(float, explicit_bounds)
        if not (xmin < xmax and ymin < ymax):
            raise ValueError("bounds must satisfy xmin < xmax and ymin < ymax")
        return (xmin, xmax, ymin, ymax), "explicit"
    finite = [geom_xy_aabb(model, data, geom_id) for geom_id in floor_geoms]
    finite = [bounds for bounds in finite if bounds is not None]
    if finite:
        return (
            min(bounds[0] for bounds in finite),
            max(bounds[1] for bounds in finite),
            min(bounds[2] for bounds in finite),
            max(bounds[3] for bounds in finite),
        ), "floor_geoms"
    extent = max(float(model.stat.extent), 1.0)
    center = model.stat.center
    return (
        float(center[0] - extent), float(center[0] + extent),
        float(center[1] - extent), float(center[1] + extent),
    ), "model_stat_extent"


def _geom_mask(model, data, geom_id, grid_x, grid_y):
    geom_type = model.geom_type[geom_id]
    if geom_type == mujoco.mjtGeom.mjGEOM_PLANE:
        return np.ones(grid_x.shape, dtype=bool)
    position = data.geom_xpos[geom_id]
    if geom_type == mujoco.mjtGeom.mjGEOM_BOX and _horizontal_box(
            model, data, geom_id):
        rotation = data.geom_xmat[geom_id].reshape(3, 3)
        dx, dy = grid_x - position[0], grid_y - position[1]
        local_x = rotation[0, 0] * dx + rotation[1, 0] * dy
        local_y = rotation[0, 1] * dx + rotation[1, 1] * dy
        size = model.geom_size[geom_id]
        return (np.abs(local_x) <= size[0]) & (np.abs(local_y) <= size[1])
    aabb = geom_xy_aabb(model, data, geom_id)
    if aabb is None:
        return np.zeros(grid_x.shape, dtype=bool)
    return ((grid_x >= aabb[0]) & (grid_x <= aabb[1])
            & (grid_y >= aabb[2]) & (grid_y <= aabb[3]))


def _disk_offsets(radius_cells: int):
    if radius_cells <= 0:
        return [(0, 0)]
    return [
        (row, col)
        for row in range(-radius_cells, radius_cells + 1)
        for col in range(-radius_cells, radius_cells + 1)
        if row * row + col * col <= radius_cells * radius_cells
    ]


def _shift_or(mask, offsets, *, outside_value=False):
    rows, cols = mask.shape
    radius = max(max(abs(row), abs(col)) for row, col in offsets)
    padded = np.pad(mask, radius, constant_values=outside_value)
    out = np.zeros_like(mask)
    for row, col in offsets:
        out |= padded[
            radius + row:radius + row + rows,
            radius + col:radius + col + cols,
        ]
    return out


def _erode(mask, offsets):
    # Erosion via complement dilation; outside the known floor is occupied.
    return ~_shift_or(~mask, offsets, outside_value=True)


def build_navigation_grid(
    scene_path: str | Path,
    *,
    resolution: float = 0.05,
    robot_radius: float = 0.25,
    margin: float = 0.0,
    robot_height: float = 1.8,
    bounds=None,
    key: str | None = None,
) -> NavigationGrid:
    scene = Path(scene_path).resolve()
    if resolution <= 0 or robot_radius < 0 or margin < 0:
        raise ValueError("resolution must be positive; radius/margin non-negative")
    model = mujoco.MjModel.from_xml_path(str(scene))
    data = mujoco.MjData(model)
    if key:
        key_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_KEY, key)
        if key_id < 0:
            raise ValueError(f"unknown keyframe {key!r}")
        mujoco.mj_resetDataKeyframe(model, data, key_id)
    mujoco.mj_forward(model, data)

    floor_geoms = _find_floor_geoms(model, data)
    map_bounds, bounds_source = _infer_bounds(model, data, floor_geoms, bounds)
    xmin, xmax, ymin, ymax = map_bounds
    cols = max(1, int(np.ceil((xmax - xmin) / resolution)))
    rows = max(1, int(np.ceil((ymax - ymin) / resolution)))
    x = xmin + (np.arange(cols) + 0.5) * resolution
    y = ymin + (np.arange(rows) + 0.5) * resolution
    grid_x, grid_y = np.meshgrid(x, y)

    floor_mask = np.zeros((rows, cols), dtype=bool)
    for geom_id in floor_geoms:
        floor_mask |= _geom_mask(model, data, geom_id, grid_x, grid_y)

    floor_tops = []
    for geom_id in floor_geoms:
        if model.geom_type[geom_id] == mujoco.mjtGeom.mjGEOM_PLANE:
            floor_tops.append(float(data.geom_xpos[geom_id][2]))
        else:
            _low, high = _geom_z_bounds(model, data, geom_id)
            floor_tops.append(high)
    floor_z = float(np.median(floor_tops)) if floor_tops else 0.0

    floor_set = set(floor_geoms)
    obstacle_geoms = []
    raw_obstacles = np.zeros_like(floor_mask)
    for geom_id in range(model.ngeom):
        if geom_id in floor_set:
            continue
        if not (int(model.geom_contype[geom_id])
                or int(model.geom_conaffinity[geom_id])):
            continue
        if not _body_is_world_fixed(model, model.geom_bodyid[geom_id]):
            continue
        low_z, high_z = _geom_z_bounds(model, data, geom_id)
        if high_z < floor_z + 0.02 or low_z > floor_z + robot_height:
            continue
        mask = _geom_mask(model, data, geom_id, grid_x, grid_y)
        if not np.any(mask):
            continue
        raw_obstacles |= mask
        obstacle_geoms.append(geom_id)

    inflation = robot_radius + margin
    radius_cells = int(np.ceil(inflation / resolution))
    offsets = _disk_offsets(radius_cells)
    safe_floor = _erode(floor_mask, offsets)
    inflated_obstacles = _shift_or(raw_obstacles, offsets)
    navigable = safe_floor & ~inflated_obstacles

    geom_name = lambda geom_id: (
        _object_name(model, mujoco.mjtObj.mjOBJ_GEOM, geom_id)
        or f"geom#{geom_id}")
    return NavigationGrid(
        scene=scene,
        resolution=float(resolution),
        robot_radius=float(robot_radius),
        margin=float(margin),
        bounds=tuple(float(value) for value in map_bounds),
        x=x,
        y=y,
        floor_mask=floor_mask,
        raw_obstacle_mask=raw_obstacles,
        navigable=navigable,
        floor_geom_names=[geom_name(geom_id) for geom_id in floor_geoms],
        obstacle_geom_names=[geom_name(geom_id) for geom_id in obstacle_geoms],
        bounds_source=bounds_source,
    )


def write_navigation_grid(grid: NavigationGrid, out_prefix: str | Path | None = None):
    prefix = Path(out_prefix) if out_prefix else grid.scene.with_suffix("")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    npz_path = prefix.with_name(prefix.name + ".navgrid.npz")
    json_path = prefix.with_name(prefix.name + ".navgrid.json")
    png_path = prefix.with_name(prefix.name + ".floorplan.png")
    np.savez_compressed(
        npz_path,
        navigable=grid.navigable,
        occupied=grid.occupied,
        floor_mask=grid.floor_mask,
        raw_obstacle_mask=grid.raw_obstacle_mask,
        x=grid.x,
        y=grid.y,
        bounds=np.asarray(grid.bounds, dtype=float),
        resolution=np.asarray(grid.resolution),
        robot_radius=np.asarray(grid.robot_radius),
        margin=np.asarray(grid.margin),
    )
    metadata = {
        "schema_version": 1,
        "scene": str(grid.scene),
        "npz": npz_path.name,
        "png": png_path.name,
        "resolution": grid.resolution,
        "robot_radius": grid.robot_radius,
        "margin": grid.margin,
        "inflation": grid.robot_radius + grid.margin,
        "bounds": list(grid.bounds),
        "bounds_source": grid.bounds_source,
        "shape": list(grid.navigable.shape),
        "axis_convention": "array[row_y, col_x], row 0 at ymin; PNG origin lower",
        "occupied_value": True,
        "floor_geoms": grid.floor_geom_names,
        "obstacle_geom_count": len(grid.obstacle_geom_names),
        "static_layer_policy": (
            "world-fixed collision geoms only; robots, free objects, and "
            "articulated bodies excluded"
        ),
    }
    json_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap

    # 0 outside floor, 1 blocked/inflated, 2 navigable, 3 raw furniture/walls.
    image = np.zeros(grid.navigable.shape, dtype=np.uint8)
    image[grid.floor_mask] = 1
    image[grid.navigable] = 2
    image[grid.raw_obstacle_mask] = 3
    colors = ListedColormap(["#16191d", "#b9bec4", "#dcecf2", "#3e454c"])
    fig, ax = plt.subplots(figsize=(9, 9))
    ax.imshow(
        image,
        origin="lower",
        extent=grid.bounds,
        interpolation="nearest",
        cmap=colors,
        vmin=0,
        vmax=3,
    )
    ax.set_aspect("equal")
    ax.set_xlabel("world x (m)")
    ax.set_ylabel("world y (m)")
    ax.set_title(
        f"{grid.scene.name} static navigation map\n"
        f"free space (blue), inflated exclusion (gray), furniture/walls (dark) | "
        f"resolution={grid.resolution:.2f}m inflation="
        f"{grid.robot_radius + grid.margin:.2f}m"
    )
    ax.grid(True, alpha=0.15)
    fig.savefig(png_path, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return {"npz": npz_path, "json": json_path, "png": png_path}


def navigation_grid_paths(scene_path: str | Path):
    """Canonical cache artifacts beside a scene XML."""
    prefix = Path(scene_path).resolve().with_suffix("")
    return {
        "npz": prefix.with_name(prefix.name + ".navgrid.npz"),
        "json": prefix.with_name(prefix.name + ".navgrid.json"),
        "png": prefix.with_name(prefix.name + ".floorplan.png"),
    }


def load_navigation_grid(scene_path: str | Path) -> NavigationGrid:
    """Load a previously generated grid without loading the MuJoCo scene."""
    scene = Path(scene_path).resolve()
    paths = navigation_grid_paths(scene)
    metadata = json.loads(paths["json"].read_text(encoding="utf-8"))
    with np.load(paths["npz"], allow_pickle=False) as arrays:
        return NavigationGrid(
            scene=scene,
            resolution=float(arrays["resolution"]),
            robot_radius=float(arrays["robot_radius"]),
            margin=float(arrays["margin"]),
            bounds=tuple(float(value) for value in arrays["bounds"]),
            x=arrays["x"].copy(),
            y=arrays["y"].copy(),
            floor_mask=arrays["floor_mask"].astype(bool, copy=True),
            raw_obstacle_mask=arrays["raw_obstacle_mask"].astype(
                bool, copy=True),
            navigable=arrays["navigable"].astype(bool, copy=True),
            floor_geom_names=list(metadata.get("floor_geoms", [])),
            obstacle_geom_names=list(metadata.get("obstacle_geoms", [])),
            bounds_source=str(metadata.get("bounds_source", "cached")),
        )


def ensure_navigation_grid(
    scene_path: str | Path,
    *,
    key: str | None = None,
) -> NavigationGrid:
    """Return the scene-level grid cache, rebuilding it only when necessary.

    A cache is stale when either canonical artifact is absent or the scene XML
    is newer than the NPZ/metadata. The in-process cache is keyed by the NPZ
    modification time, so repeated navigate calls are memory-only lookups.
    """
    scene = Path(scene_path).resolve()
    with _GRID_CACHE_LOCK:
        paths = navigation_grid_paths(scene)
        scene_mtime = scene.stat().st_mtime_ns
        disk_fresh = (
            paths["npz"].exists()
            and paths["json"].exists()
            and paths["npz"].stat().st_mtime_ns >= scene_mtime
            and paths["json"].stat().st_mtime_ns >= scene_mtime
        )
        if not disk_fresh:
            # Preserve per-scene tuning across XML edits. In particular,
            # layout012_preparing needs a 0.45 m footprint while older study
            # scenes retain validated 0.25 m manipulation standoffs.
            rebuild_kwargs = {}
            if paths["json"].exists():
                try:
                    metadata = json.loads(paths["json"].read_text("utf-8"))
                    rebuild_kwargs = {
                        "resolution": float(metadata["resolution"]),
                        "robot_radius": float(metadata["robot_radius"]),
                        "margin": float(metadata["margin"]),
                    }
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    rebuild_kwargs = {}
            grid = build_navigation_grid(scene, key=key, **rebuild_kwargs)
            write_navigation_grid(grid)
        cache_stamp = paths["npz"].stat().st_mtime_ns
        cached = _GRID_CACHE.get(scene)
        if cached is None or cached[0] != cache_stamp:
            _GRID_CACHE[scene] = (cache_stamp, load_navigation_grid(scene))
        return _GRID_CACHE[scene][1]


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", type=Path)
    parser.add_argument("--resolution", type=float, default=0.05)
    # Individual scenes can be generated with a larger footprint and retain
    # that value across later XML-triggered rebuilds in ensure_navigation_grid.
    parser.add_argument("--robot-radius", type=float, default=0.25)
    parser.add_argument("--margin", type=float, default=0.0)
    parser.add_argument("--robot-height", type=float, default=1.8)
    parser.add_argument("--key")
    parser.add_argument(
        "--bounds", nargs=4, type=float, metavar=("XMIN", "XMAX", "YMIN", "YMAX"))
    parser.add_argument("--out-prefix", type=Path)
    return parser.parse_args()


def main():
    args = _parse_args()
    grid = build_navigation_grid(
        args.scene,
        resolution=args.resolution,
        robot_radius=args.robot_radius,
        margin=args.margin,
        robot_height=args.robot_height,
        bounds=args.bounds,
        key=args.key,
    )
    paths = write_navigation_grid(grid, args.out_prefix)
    free = int(np.count_nonzero(grid.navigable))
    total = int(grid.navigable.size)
    print(
        f"grid {grid.navigable.shape[1]}x{grid.navigable.shape[0]} "
        f"free={free}/{total} ({100.0 * free / max(total, 1):.1f}%)"
    )
    for kind, path in paths.items():
        print(f"wrote {kind}: {path}")


if __name__ == "__main__":
    main()
