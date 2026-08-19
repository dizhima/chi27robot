"""Deterministic application of the conflict-resolution repair toolset
(design doc §3c). Each function edits a flat AuthoredPlan
(`{robot: [step, ...]}` — the exact shape `compile_plan`'s `completed` output
already is, so it round-trips straight back into the next recompile) and
raises `ToolError` on anything that isn't a legal, bounded edit. Everything
except `replan_path` is pure plan-graph surgery. The latter combines compiler
occupancy with the cached static navigation grid; the LLM still supplies only
symbolic ids/robots, never coordinates (design doc §3e).
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from mujoco_skills.pipeline.build_navigation_grid import (
    NavigationGrid, ensure_navigation_grid,
)
from mujoco_skills.pipeline.navigation_planner import (
    NavigationPathError, plan_grid_route,
)
# `STANDOFF_MIN_DIST` also lives in skill_generators as the detection
# threshold; importing it (not redefining it) keeps the repair clearance
# margin and the conflict threshold from drifting apart.
from mujoco_skills.skills.skill_generators import STANDOFF_MIN_DIST


_USE_STEP_VIA_POINTS = object()


# Yield search is deliberately bounded.  The static grids used by the study
# scenes contain thousands of cells; checking the nearest few hundred gives a
# useful local passing bay without turning one resolver focus into an
# unbounded all-pairs path-planning problem.
MAX_YIELD_CELL_CHECKS = 256
MAX_FEASIBLE_YIELD_CELLS = 12
# Keep a yielding robot materially outside the congested work area rather
# than accepting the first cell just beyond the collision threshold.
# 0.8 m is the normal base-conflict threshold, so an extra 1.7 m makes the
# deadlock parking pose at least 2.5 m from the congested poses.  The grid
# half-diagonal below remains an additional rasterisation safety margin.
YIELD_PARKING_EXTRA_CLEARANCE = 1.70

class ToolError(Exception):
    """A tool call that cannot be applied as given — the round rejects it
    with this message rather than corrupting the plan."""


def _find_step(plan, step_id):
    for robot, steps in plan.items():
        for step in steps:
            if step["id"] == step_id:
                return robot, step
    raise ToolError(f"unknown step id {step_id!r}")


def _sequential_edges(plan):
    """Per-robot program order as dependency edges: step depends on the
    previous step on the same robot. Not authored into `after` (design
    doc's compiler comment: "per-robot ordering is already enforced by
    robot availability") but real for cycle-checking a NEW cross-robot edge."""
    edges = {}
    for steps in plan.values():
        for prev, cur in zip(steps, steps[1:]):
            edges.setdefault(cur["id"], set()).add(prev["id"])
    return edges


def _has_cycle(edges):
    """`edges`: id -> set of ids it depends on (must run after). DFS for a
    cycle in that dependency graph."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}

    def visit(node):
        color[node] = GRAY
        for dep in edges.get(node, ()):
            state = color.get(dep, WHITE)
            if state == GRAY:
                return True
            if state == WHITE and visit(dep):
                return True
        color[node] = BLACK
        return False

    return any(color.get(node, WHITE) == WHITE and visit(node) for node in edges)


def apply_add_after(plan, dep_graph, step_id, after_step_id):
    """`step_id` waits until `after_step_id` ends. `dep_graph`: the current
    round's authored+container after-edges (payload's `dep_graph`), used
    only to pre-check the new edge won't create a cycle together with
    per-robot sequential order — the actual `after` list lives on the step
    itself and is what compile_plan re-derives dependencies from."""
    if step_id == after_step_id:
        raise ToolError("a step cannot wait on itself")
    _, step = _find_step(plan, step_id)
    _find_step(plan, after_step_id)  # existence check only
    if str(step.get("op", "")).lower() not in ("navigate", "go_to"):
        raise ToolError(
            "add_after target must be navigate/go_to; "
            f"got {step_id!r} op={step.get('op')!r}")

    edges = {sid: set(deps) for sid, deps in dep_graph.items()}
    edges.setdefault(step_id, set()).add(after_step_id)
    for dep_id, deps in _sequential_edges(plan).items():
        edges.setdefault(dep_id, set()).update(deps)
    if _has_cycle(edges):
        raise ToolError(
            f"add_after({step_id}, {after_step_id}) would create a "
            "dependency cycle")

    after = list(step.get("after", []))
    if after_step_id not in after:
        after.append(after_step_id)
    step["after"] = after
    return step_id


def apply_insert_go_to(plan, robot, rest_xy):
    """Retire `robot` to its rest point, appending a navigate+reset leg.
    Idempotent by construction: the departure (navigate) step's id is a
    pure function of `robot`, so a repeat call (or an add_after issued in
    the same round that references it) finds the same step rather than
    piling up duplicates."""
    if robot not in plan:
        raise ToolError(f"unknown robot {robot!r}")
    if rest_xy is None:
        raise ToolError(f"no rest point known for {robot!r}")
    departure_id = f"{robot}#go_to_rest"
    steps = plan[robot]
    if any(s["id"] == departure_id for s in steps):
        return departure_id  # already retired this round/plan
    xy = [round(float(rest_xy[0]), 3), round(float(rest_xy[1]), 3)]
    steps.append({
        "id": departure_id, "op": "navigate", "target": None,
        # Returning home is a full pose requirement, not only an XY target.
        # final_yaw deliberately differs from arrival_yaw: navigate follows its
        # normal travel-facing route all the way to the endpoint, then corrects
        # orientation in place to the robot's qpos0 yaw.
        "standoff": xy, "final_yaw": "home", "after": [],
        "group": departure_id,
    })
    steps.append({
        "id": f"{robot}#go_to_rest:reset", "op": "reset",
        # Navigation already returned the base to its exact home pose. Reset
        # only the arm here; another base retreat can push a boundary-adjacent
        # home position outside the scene.
        "retreat": 0.0, "preserve_yaw": True, "after": [],
        "group": departure_id,
    })
    return departure_id


def apply_handoff_terminal_close(plan, close_step_id, to_robot):
    """Move an entire terminal close task to the robot that finished using
    the container last.

    This is deliberately narrower than generic reassignment.  The close
    step's whole ``group`` (approach/close/reset) must be the source robot's
    terminal suffix, and no member may be explicitly robot-locked. Moving
    the group as a unit preserves its internal ordering and authored
    dependencies while avoiding a mid-program splice.
    """
    from_robot, close_step = _find_step(plan, close_step_id)
    op = str(close_step.get("op", "")).lower()
    if not op.startswith("close"):
        raise ToolError(
            f"{close_step_id!r} is not a close step "
            f"(op={close_step.get('op')!r})")
    if to_robot not in plan:
        raise ToolError(f"unknown robot {to_robot!r}")
    if from_robot == to_robot:
        raise ToolError(
            f"{close_step_id!r} is already assigned to {to_robot!r}")

    group = close_step.get("group") or close_step_id
    source = plan[from_robot]
    member_indices = [
        i for i, step in enumerate(source)
        if (step.get("group") or step["id"]) == group
    ]
    if not member_indices:
        raise ToolError(f"close group {group!r} has no members")
    first = min(member_indices)
    expected = list(range(first, first + len(member_indices)))
    trailing = source[first + len(member_indices):]
    generated_departure_group = f"{from_robot}#go_to_rest"
    if member_indices != expected or any(
        (step.get("group") or step.get("id")) != generated_departure_group
        for step in trailing
    ):
        raise ToolError(
            f"close group {group!r} is not a terminal suffix (optionally "
            f"followed by its accepted rest departure) of "
            f"{from_robot!r}")

    members = source[first:first + len(member_indices)]
    locked = [step["id"] for step in members if step.get("robot_locked")]
    if locked:
        raise ToolError(
            f"close group {group!r} is robot-locked "
            f"(locked steps: {locked})")

    del source[first:first + len(member_indices)]
    for step in members:
        step["robot"] = to_robot
    plan[to_robot].extend(members)
    return group


def _segment_position(segment, time_value):
    t0, t1 = float(segment["t0"]), float(segment["t1"])
    p0 = np.asarray(segment["p0"], dtype=float)[:2]
    p1 = np.asarray(segment["p1"], dtype=float)[:2]
    if t1 <= t0 + 1e-12:
        return p1
    fraction = min(1.0, max(0.0, (time_value - t0) / (t1 - t0)))
    return p0 + fraction * (p1 - p0)


def _swept_obstacle_mask(grid, segments, window):
    """Rasterise conflict-window capsules around an avoided base trajectory."""
    window_lo, window_hi = (float(window[0]), float(window[1]))
    mask = np.zeros_like(grid.navigable, dtype=bool)
    # Cover the whole grid cell, rather than only its centre, so a simplified
    # line segment cannot clip the edge of the 0.8 m base-separation boundary.
    clearance = STANDOFF_MIN_DIST + grid.resolution / np.sqrt(2.0)
    for segment in segments:
        lo = max(window_lo, float(segment["t0"]))
        hi = min(window_hi, float(segment["t1"]))
        if hi < lo - 1e-12:
            continue
        p0 = _segment_position(segment, lo)
        p1 = _segment_position(segment, hi)
        xmin, xmax = sorted((p0[0], p1[0]))
        ymin, ymax = sorted((p0[1], p1[1]))
        cols = np.flatnonzero(
            (grid.x >= xmin - clearance) & (grid.x <= xmax + clearance))
        rows = np.flatnonzero(
            (grid.y >= ymin - clearance) & (grid.y <= ymax + clearance))
        if not len(rows) or not len(cols):
            continue
        xx, yy = np.meshgrid(grid.x[cols], grid.y[rows])
        points = np.stack((xx, yy), axis=-1)
        delta = p1 - p0
        length_sq = float(np.dot(delta, delta))
        if length_sq <= 1e-18:
            distance_sq = np.sum((points - p0) ** 2, axis=-1)
        else:
            fraction = np.clip(
                np.sum((points - p0) * delta, axis=-1) / length_sq,
                0.0, 1.0,
            )
            closest = p0 + fraction[..., None] * delta
            distance_sq = np.sum((points - closest) ** 2, axis=-1)
        region = np.ix_(rows, cols)
        mask[region] |= distance_sq <= clearance * clearance
    return mask


def _stationary_obstacle_mask(grid, xy):
    """Rasterise one stationary base with the resolver's normal clearance."""
    point = np.asarray(xy, dtype=float)[:2]
    return _swept_obstacle_mask(
        grid,
        [{"t0": 0.0, "t1": 1.0, "p0": point, "p1": point}],
        [0.0, 1.0],
    )


def _grid_with_dynamic_obstacle(grid, mask):
    """Add a robot mask without allowing endpoint connectors through it."""
    return replace(
        grid,
        navigable=grid.navigable & ~mask,
        raw_obstacle_mask=grid.raw_obstacle_mask | mask,
    )


def _route_length(route):
    return sum(
        float(np.linalg.norm(np.asarray(end) - np.asarray(start)))
        for start, end in zip(route, route[1:])
    )


def _compiled_route_start(step, mover_step_id, timelines):
    route = step.get("route")
    if route:
        return np.asarray(route[0], dtype=float)[:2]
    for segment in timelines:
        if segment.get("step") == mover_step_id:
            return np.asarray(segment["p0"], dtype=float)[:2]
    raise ToolError(
        f"no compiled route start found for {mover_step_id!r}; recompile first")


def _compiled_route_goal(step, mover_step_id, timelines):
    """Return the compiled chassis destination for a navigation step.

    Named-target ``standoff`` values use the manipulation base/mount contract,
    while navigation routes and occupancy timelines describe the physical
    chassis centre.  Conflict repair plans in navigation-grid coordinates, so
    feeding it ``standoff`` can put its final via point on the wrong side of an
    inflated obstacle.
    """
    route = step.get("route")
    if route:
        return np.asarray(route[-1], dtype=float)[:2]
    matching = [
        segment for segment in timelines
        if segment.get("step") == mover_step_id
    ]
    if matching:
        return np.asarray(matching[-1]["p1"], dtype=float)[:2]
    raise ToolError(
        f"no compiled route goal found for {mover_step_id!r}; recompile first")


def _moving_step_geometry(plan, step_id, compile_result):
    robot, step = _find_step(plan, step_id)
    if str(step.get("op", "")).lower() not in ("navigate", "go_to"):
        raise ToolError(f"{step_id!r} is not a moving navigate/go_to step")
    timelines = (compile_result.get("base_timelines") or {}).get(robot) or []
    start = _compiled_route_start(step, step_id, timelines)
    destination = _compiled_route_goal(step, step_id, timelines)
    return {
        "robot": robot,
        "step": step,
        "start": np.asarray(start, dtype=float)[:2],
        "goal": np.asarray(destination, dtype=float)[:2],
    }


def _navigation_grid(compile_result, navigation_grid=None):
    if navigation_grid is not None:
        return navigation_grid
    scene_xml = compile_result.get("scene_xml")
    if not scene_xml:
        raise ToolError("compile result has no scene_xml for navigation")
    try:
        return ensure_navigation_grid(scene_xml)
    except Exception as exc:  # deterministic analysis failure
        raise ToolError(f"could not load scene navigation grid: {exc}") from exc


def _plan_while_stationary(grid, mover, stationary_xy):
    temporary = _grid_with_dynamic_obstacle(
        grid, _stationary_obstacle_mask(grid, stationary_xy))
    return plan_grid_route(
        temporary,
        mover["start"],
        mover["goal"],
        via_points=mover["step"].get("via_points"),
    )


def _best_yield_maneuver(grid, yielding, winner):
    """Find a three-phase parking maneuver for one priority ordering.

    Phase 1 moves ``yielding`` to a temporary free cell while ``winner`` is
    stationary.  Phase 2 lets ``winner`` complete while the yielding robot is
    parked.  Phase 3 resumes the yielding robot while the winner dwells at its
    goal.  Every phase uses the same navgrid planner as ordinary navigate.
    """
    free = np.argwhere(grid.navigable)
    if free.size == 0:
        return None
    points = np.column_stack((grid.x[free[:, 1]], grid.y[free[:, 0]]))
    clearance = (
        STANDOFF_MIN_DIST
        + YIELD_PARKING_EXTRA_CLEARANCE
        + grid.resolution / np.sqrt(2.0)
    )
    distances = np.linalg.norm(points - yielding["start"], axis=1)
    safe = distances >= clearance
    # A parking pose must not occupy either pose where the winner remains
    # stationary during phase 1 or phase 3.
    safe &= np.linalg.norm(points - winner["start"], axis=1) >= clearance
    safe &= np.linalg.norm(points - winner["goal"], axis=1) >= clearance
    indices = np.flatnonzero(safe)
    if not len(indices):
        return None
    indices = indices[np.argsort(distances[indices], kind="stable")]

    phase1_grid = _grid_with_dynamic_obstacle(
        grid, _stationary_obstacle_mask(grid, winner["start"]))
    phase3_grid = _grid_with_dynamic_obstacle(
        grid, _stationary_obstacle_mask(grid, winner["goal"]))
    best = None
    feasible = 0
    for index in indices[:MAX_YIELD_CELL_CHECKS]:
        parking = points[index]
        try:
            yield_route = plan_grid_route(
                phase1_grid, yielding["start"], parking)
            phase2_grid = _grid_with_dynamic_obstacle(
                grid, _stationary_obstacle_mask(grid, parking))
            winner_route = plan_grid_route(
                phase2_grid,
                winner["start"],
                winner["goal"],
                via_points=winner["step"].get("via_points"),
            )
            resume_route = plan_grid_route(
                phase3_grid,
                parking,
                yielding["goal"],
                via_points=yielding["step"].get("via_points"),
            )
        except NavigationPathError:
            continue
        feasible += 1
        score = (
            _route_length(yield_route)
            + _route_length(winner_route)
            + _route_length(resume_route)
        )
        candidate = {
            "parking_xy": parking,
            "yield_route": yield_route,
            "winner_route": winner_route,
            "resume_route": resume_route,
            "estimated_distance": score,
        }
        if best is None or score < best["estimated_distance"] - 1e-9:
            best = candidate
        if feasible >= MAX_FEASIBLE_YIELD_CELLS:
            break
    return best


def compute_go_away_maneuver(
    plan,
    occupant_robot,
    winner_step_id,
    conflict,
    compile_result,
    *,
    resume_goal=None,
    navigation_grid: NavigationGrid | None = None,
):
    """Choose a temporary parking point for a dwelling conflict occupant.

    This is the two-phase subset of the existing yield search: first the
    occupant must reach a free parking cell while the winner stays at its
    route start, then the winner must be able to complete while the occupant
    remains parked. When the occupant has a later navigation destination,
    ``resume_goal`` adds the static parking-to-destination route to the score,
    so a safe point already lying in that direction beats an equally safe
    detour. No fixed home/rest coordinate is assumed here.
    """
    party = next((
        candidate for candidate in (conflict.get("parties") or [])
        if candidate.get("robot") == occupant_robot
    ), None)
    if party is None or party.get("motion") == "moving":
        raise ToolError(
            f"{occupant_robot!r} is not the dwelling conflict occupant")

    winner = _moving_step_geometry(plan, winner_step_id, compile_result)
    timelines = (compile_result.get("base_timelines") or {}).get(
        occupant_robot) or []
    occupied_segments = [
        segment for segment in timelines
        if segment.get("step") == party.get("step")
    ]
    if not occupied_segments:
        raise ToolError(
            f"no compiled occupancy found for {occupant_robot!r} "
            f"step {party.get('step')!r}")
    occupant_start = np.asarray(
        occupied_segments[-1]["p1"], dtype=float)[:2]
    grid = _navigation_grid(compile_result, navigation_grid)

    free = np.argwhere(grid.navigable)
    if free.size == 0:
        raise ToolError("navigation grid has no temporary parking cells")
    points = np.column_stack((grid.x[free[:, 1]], grid.y[free[:, 0]]))
    clearance = (
        STANDOFF_MIN_DIST
        + YIELD_PARKING_EXTRA_CLEARANCE
        + grid.resolution / np.sqrt(2.0)
    )
    distances = np.linalg.norm(points - occupant_start, axis=1)
    safe = distances >= clearance
    safe &= np.linalg.norm(points - winner["start"], axis=1) >= clearance
    safe &= np.linalg.norm(points - winner["goal"], axis=1) >= clearance
    indices = np.flatnonzero(safe)
    if not len(indices):
        raise ToolError("no clearance-safe temporary parking point")
    resume_xy = (
        np.asarray(resume_goal, dtype=float)[:2]
        if resume_goal is not None else None
    )
    # Search promising through-points first. This matters because route
    # feasibility is intentionally bounded below; nearest-to-start alone can
    # exhaust the budget on points in the opposite direction of the robot's
    # next task.
    lower_bound = distances.copy()
    if resume_xy is not None:
        lower_bound += np.linalg.norm(points - resume_xy, axis=1)
    indices = indices[np.argsort(lower_bound[indices], kind="stable")]

    departure_grid = _grid_with_dynamic_obstacle(
        grid, _stationary_obstacle_mask(grid, winner["start"]))
    best = None
    feasible = 0
    for index in indices[:MAX_YIELD_CELL_CHECKS]:
        parking = points[index]
        try:
            departure_route = plan_grid_route(
                departure_grid, occupant_start, parking)
            winner_grid = _grid_with_dynamic_obstacle(
                grid, _stationary_obstacle_mask(grid, parking))
            winner_route = plan_grid_route(
                winner_grid,
                winner["start"],
                winner["goal"],
                via_points=winner["step"].get("via_points"),
            )
            resume_route = (
                plan_grid_route(grid, parking, resume_xy)
                if resume_xy is not None else None
            )
        except NavigationPathError:
            continue
        feasible += 1
        score = (
            _route_length(departure_route)
            + _route_length(winner_route)
            + (_route_length(resume_route) if resume_route is not None else 0.0)
        )
        candidate = {
            "parking_xy": parking,
            "departure_route": departure_route,
            "winner_route": winner_route,
            "resume_route": resume_route,
            "resume_goal": resume_xy,
            "estimated_distance": score,
        }
        if best is None or score < best["estimated_distance"] - 1e-9:
            best = candidate
        if feasible >= MAX_FEASIBLE_YIELD_CELLS:
            break
    if best is None:
        raise ToolError("no feasible temporary go-away maneuver")
    return best


def analyze_spatial_deadlock(
    plan,
    conflict,
    compile_result,
    *,
    navigation_grid: NavigationGrid | None = None,
):
    """Detect a two-mover priority deadlock and enumerate yield directions.

    This is a cheap navgrid preflight, not a compiler or MuJoCo rollout.  A
    priority ordering is feasible only when one mover can reach its goal while
    the other remains at its current pose.  If neither ordering works, the
    original two moves require an intermediate parking action.
    """
    if conflict.get("kind") != "path" or conflict.get("class") != "both_moving":
        return {"detected": False, "can_go_first": {}, "yield_candidates": []}
    parties = [
        party for party in (conflict.get("parties") or [])
        if party.get("motion") == "moving"
    ]
    if len(parties) != 2 or parties[0].get("robot") == parties[1].get("robot"):
        return {"detected": False, "can_go_first": {}, "yield_candidates": []}

    grid = _navigation_grid(compile_result, navigation_grid)
    geometry = {
        party["step"]: _moving_step_geometry(
            plan, party["step"], compile_result)
        for party in parties
    }
    can_go_first = {}
    for party, other in ((parties[0], parties[1]), (parties[1], parties[0])):
        try:
            _plan_while_stationary(
                grid, geometry[party["step"]], geometry[other["step"]]["start"])
            can_go_first[party["step"]] = True
        except NavigationPathError:
            can_go_first[party["step"]] = False
    if any(can_go_first.values()):
        return {
            "detected": False,
            "can_go_first": can_go_first,
            "yield_candidates": [],
        }

    candidates = []
    for yielding_party, winner_party in (
        (parties[0], parties[1]), (parties[1], parties[0])
    ):
        yielding = geometry[yielding_party["step"]]
        winner = geometry[winner_party["step"]]
        maneuver = _best_yield_maneuver(grid, yielding, winner)
        if maneuver is None:
            continue
        candidates.append({
            "yielding_step": yielding_party["step"],
            "winner_step": winner_party["step"],
            "yielding_robot": yielding["robot"],
            "winner_robot": winner["robot"],
            **maneuver,
        })
    candidates.sort(key=lambda value: (
        value["estimated_distance"], value["yielding_robot"],
        value["yielding_step"],
    ))
    return {
        "detected": True,
        "can_go_first": can_go_first,
        "yield_candidates": candidates,
    }


def apply_insert_yield(
    plan,
    yielding_step_id,
    winner_step_id,
    conflict,
    compile_result,
    *,
    navigation_grid: NavigationGrid | None = None,
):
    """Insert a deterministic park/pass/resume topology for a deadlock."""
    analysis = analyze_spatial_deadlock(
        plan, conflict, compile_result, navigation_grid=navigation_grid)
    if not analysis.get("detected"):
        raise ToolError("focus is not a spatial priority deadlock")
    maneuver = next((
        candidate for candidate in analysis.get("yield_candidates", [])
        if candidate["yielding_step"] == yielding_step_id
        and candidate["winner_step"] == winner_step_id
    ), None)
    if maneuver is None:
        raise ToolError(
            f"no yield maneuver for {yielding_step_id!r} before "
            f"{winner_step_id!r}")

    yielding_robot, yielding_step = _find_step(plan, yielding_step_id)
    winner_robot, winner_step = _find_step(plan, winner_step_id)
    if yielding_robot == winner_robot:
        raise ToolError("yielding and winner steps must belong to different robots")
    yield_id = f"{yielding_step_id}#yield"
    if any(step.get("id") == yield_id for step in plan[yielding_robot]):
        raise ToolError(f"yield step {yield_id!r} already exists")

    original_after = list(yielding_step.get("after", []))
    yield_route = maneuver["yield_route"]
    yield_step = {
        "id": yield_id,
        "op": "navigate",
        "target": None,
        "standoff": [
            round(float(maneuver["parking_xy"][0]), 3),
            round(float(maneuver["parking_xy"][1]), 3),
        ],
        "via_points": [
            [round(float(point[0]), 3), round(float(point[1]), 3)]
            for point in yield_route[1:-1]
        ],
        "align_final_yaw": False,
        "after": original_after,
        "group": yield_id,
    }
    index = plan[yielding_robot].index(yielding_step)
    plan[yielding_robot].insert(index, yield_step)

    yielding_step["after"] = [winner_step_id]
    winner_after = list(winner_step.get("after", []))
    if yield_id not in winner_after:
        winner_after.append(yield_id)
    winner_step["after"] = winner_after
    yielding_step["via_points"] = [
        [round(float(point[0]), 3), round(float(point[1]), 3)]
        for point in maneuver["resume_route"][1:-1]
    ]
    yielding_step.pop("route", None)
    winner_step["via_points"] = [
        [round(float(point[0]), 3), round(float(point[1]), 3)]
        for point in maneuver["winner_route"][1:-1]
    ]
    winner_step.pop("route", None)

    edges = {
        step["id"]: set(step.get("after", []))
        for steps in plan.values() for step in steps
    }
    for step_id, dependencies in _sequential_edges(plan).items():
        edges.setdefault(step_id, set()).update(dependencies)
    if _has_cycle(edges):
        raise ToolError("insert_yield would create a dependency cycle")
    return yield_id


def apply_replan_path(
    plan,
    mover_step_id,
    avoid_robot,
    conflict,
    compile_result,
    *,
    navigation_grid: NavigationGrid | None = None,
    authored_via_points=_USE_STEP_VIA_POINTS,
):
    """Reroute one moving step around another robot's swept occupancy.

    The avoided robot's exact compiler-produced occupancy over the conflict
    window becomes a temporary obstacle layer on top of the scene navgrid.
    A* computes a complete static-safe route; its interior points are stored as
    ``via_points`` so the ordinary navigate compiler reproduces and verifies
    the route. ``authored_via_points`` lets an incremental caller supply the
    original user constraints after an earlier repair has temporarily stored
    generated geometry in the same public field.  Generated detours must never
    feed back as hard anchors into a later reroute. No route-length cap is
    imposed: no-path is the only spatial failure, and the resolver can fall
    back to temporal ordering next round.
    """
    robot, step = _find_step(plan, mover_step_id)
    if step["op"] not in ("navigate", "go_to"):
        raise ToolError(
            f"{mover_step_id!r} is not a moving step (op={step['op']!r})")

    parties = conflict.get("parties") or []
    mover_party = next(
        (party for party in parties
         if party.get("step") == mover_step_id
         or party.get("id") == mover_step_id),
        None,
    )
    if mover_party is None or mover_party.get("motion") != "moving":
        raise ToolError(
            f"{mover_step_id!r} is not a moving party in this conflict")
    avoid_party = next(
        (party for party in parties if party.get("robot") == avoid_robot), None)
    if avoid_party is None or avoid_robot == robot:
        raise ToolError(
            f"avoid robot {avoid_robot!r} is not the other conflict party")

    window = conflict.get("window")
    timelines = compile_result.get("base_timelines") or {}
    mover_timeline = timelines.get(robot) or []
    avoid_timeline = timelines.get(avoid_robot) or []
    if not window or len(window) != 2:
        raise ToolError(
            f"replan_path needs a conflict window for {mover_step_id!r}")
    if not avoid_timeline:
        raise ToolError(f"no compiled base timeline found for {avoid_robot!r}")

    if navigation_grid is None:
        scene_xml = compile_result.get("scene_xml")
        if not scene_xml:
            raise ToolError("compile result has no scene_xml for navigation")
        try:
            navigation_grid = ensure_navigation_grid(scene_xml)
        except Exception as exc:  # deterministic tool failure, not loop failure
            raise ToolError(f"could not load scene navigation grid: {exc}") from exc

    dynamic_mask = _swept_obstacle_mask(
        navigation_grid, avoid_timeline, window)
    if not np.any(dynamic_mask):
        raise ToolError(
            f"{avoid_robot!r} has no occupancy inside conflict window {window}")
    temporary_grid = replace(
        navigation_grid,
        navigable=navigation_grid.navigable & ~dynamic_mask,
        # Endpoint connectors may cross the static inflation halo, but must
        # never tunnel through the dynamic robot obstacle.
        raw_obstacle_mask=navigation_grid.raw_obstacle_mask | dynamic_mask,
    )
    start_xy = _compiled_route_start(step, mover_step_id, mover_timeline)
    dest_xy = _compiled_route_goal(step, mover_step_id, mover_timeline)
    route_constraints = (
        step.get("via_points")
        if authored_via_points is _USE_STEP_VIA_POINTS
        else authored_via_points
    )
    try:
        route = plan_grid_route(
            temporary_grid,
            start_xy,
            np.asarray(dest_xy, dtype=float)[:2],
            via_points=route_constraints,
        )
    except NavigationPathError as exc:
        raise ToolError(f"no conflict-free alternate route: {exc}") from exc

    step["via_points"] = [
        [round(float(point[0]), 3), round(float(point[1]), 3)]
        for point in route[1:-1]
    ]
    step.pop("route", None)
    return mover_step_id
