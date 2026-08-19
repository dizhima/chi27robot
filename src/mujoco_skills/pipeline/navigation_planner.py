"""Deterministic 2-D A* planning over a cached :class:`NavigationGrid`."""

from __future__ import annotations

import heapq
import math
from dataclasses import replace

import numpy as np

from mujoco_skills.pipeline.build_navigation_grid import NavigationGrid


class NavigationPathError(ValueError):
    pass


_NEIGHBORS = (
    (-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
    (-1, -1, math.sqrt(2.0)), (-1, 1, math.sqrt(2.0)),
    (1, -1, math.sqrt(2.0)), (1, 1, math.sqrt(2.0)),
)

# A preference, not a hard constraint: a shorter segment is retained when no
# collision-free visibility path can avoid it (for example at a tight corner).
MIN_ROUTE_SEGMENT = 0.40
MAX_TRANSIT_CLEARANCE = 0.20


def _cell_center(grid: NavigationGrid, cell: tuple[int, int]) -> np.ndarray:
    row, col = cell
    return np.array([grid.x[col], grid.y[row]], dtype=float)


def _nearest_free_cell(
    grid: NavigationGrid,
    point,
    *,
    max_distance: float = 0.60,
) -> tuple[int, int]:
    direct = grid.cell_for_xy(point)
    if direct is not None and grid.navigable[direct]:
        return direct
    free = np.argwhere(grid.navigable)
    if free.size == 0:
        raise NavigationPathError("navigation grid contains no free cells")
    point = np.asarray(point, dtype=float)
    dx = grid.x[free[:, 1]] - point[0]
    dy = grid.y[free[:, 0]] - point[1]
    index = int(np.argmin(dx * dx + dy * dy))
    distance = float(math.hypot(float(dx[index]), float(dy[index])))
    if distance > max_distance:
        raise NavigationPathError(
            f"point {point.tolist()} is {distance:.3f}m from navigable space "
            f"(limit {max_distance:.3f}m)")
    return int(free[index, 0]), int(free[index, 1])


def _astar(
    navigable: np.ndarray,
    start: tuple[int, int],
    goal: tuple[int, int],
) -> list[tuple[int, int]]:
    if start == goal:
        return [start]
    rows, cols = navigable.shape
    queue = [(0.0, 0.0, start)]
    came_from: dict[tuple[int, int], tuple[int, int]] = {}
    cost = {start: 0.0}
    closed = set()
    while queue:
        _estimate, current_cost, current = heapq.heappop(queue)
        if current in closed:
            continue
        if current == goal:
            path = [current]
            while path[-1] != start:
                path.append(came_from[path[-1]])
            path.reverse()
            return path
        closed.add(current)
        row, col = current
        for dr, dc, step_cost in _NEIGHBORS:
            nr, nc = row + dr, col + dc
            if not (0 <= nr < rows and 0 <= nc < cols):
                continue
            if not navigable[nr, nc]:
                continue
            # Do not squeeze diagonally between two blocked cells.
            if dr and dc and (
                    not navigable[row, nc] or not navigable[nr, col]):
                continue
            neighbor = (nr, nc)
            candidate = current_cost + step_cost
            if candidate >= cost.get(neighbor, math.inf):
                continue
            cost[neighbor] = candidate
            came_from[neighbor] = current
            heuristic = math.hypot(goal[0] - nr, goal[1] - nc)
            heapq.heappush(
                queue, (candidate + heuristic, candidate, neighbor))
    raise NavigationPathError(
        f"no navigation path between grid cells {start} and {goal}")


def _segment_is_free(
    grid: NavigationGrid,
    start,
    end,
    mask: np.ndarray,
) -> bool:
    start = np.asarray(start, dtype=float)
    end = np.asarray(end, dtype=float)
    distance = float(np.linalg.norm(end - start))
    samples = max(1, int(math.ceil(distance / (grid.resolution * 0.35))))
    for fraction in np.linspace(0.0, 1.0, samples + 1):
        cell = grid.cell_for_xy(start + fraction * (end - start))
        if cell is None or not mask[cell]:
            return False
    return True


def _simplify_cells(
    grid: NavigationGrid,
    cells: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    if len(cells) <= 2:
        return cells

    # Dynamic programming over the A* path's visibility graph. Optimise
    # lexicographically: first minimise sub-threshold segments, then waypoint
    # count, then total distance. Unlike greedy "farthest visible", this avoids
    # producing one tiny cleanup leg immediately after a long shortcut when a
    # slightly different pair of visible corners gives more even spacing.
    best: list[tuple[tuple[int, int, float], int] | None] = [None] * len(cells)
    best[0] = ((0, 0, 0.0), -1)
    centers = [_cell_center(grid, cell) for cell in cells]
    for end in range(1, len(cells)):
        for start in range(end):
            previous = best[start]
            if previous is None:
                continue
            if not _segment_is_free(
                    grid, centers[start], centers[end], grid.navigable):
                continue
            length = float(np.linalg.norm(centers[end] - centers[start]))
            old_score = previous[0]
            score = (
                old_score[0] + int(length + 1e-9 < MIN_ROUTE_SEGMENT),
                old_score[1] + 1,
                old_score[2] + length,
            )
            if best[end] is None or score < best[end][0]:
                best[end] = (score, start)

    if best[-1] is None:
        # Consecutive A* cells are always visible, so this is defensive only.
        return cells
    indices = [len(cells) - 1]
    while indices[-1] != 0:
        indices.append(best[indices[-1]][1])
    indices.reverse()
    return [cells[index] for index in indices]


def _plan_grid_segment_core(
    grid: NavigationGrid,
    start,
    goal,
) -> list[np.ndarray]:
    start = np.asarray(start, dtype=float)[:2]
    goal = np.asarray(goal, dtype=float)[:2]
    if np.linalg.norm(goal - start) < 1e-9:
        return [start.copy()]
    direct_start_cell = grid.cell_for_xy(start)
    direct_goal_cell = grid.cell_for_xy(goal)
    start_is_free = bool(
        direct_start_cell is not None and grid.navigable[direct_start_cell])
    goal_is_free = bool(
        direct_goal_cell is not None and grid.navigable[direct_goal_cell])
    if (start_is_free and goal_is_free
            and _segment_is_free(grid, start, goal, grid.navigable)):
        return [start.copy(), goal.copy()]

    start_cell = _nearest_free_cell(grid, start)
    goal_cell = _nearest_free_cell(grid, goal)
    cells = _simplify_cells(grid, _astar(grid.navigable, start_cell, goal_cell))
    centers = [_cell_center(grid, cell) for cell in cells]
    if (start_is_free and len(centers) > 1 and _segment_is_free(
            grid, start, centers[1], grid.navigable)):
        centers.pop(0)
    if (goal_is_free and len(centers) > 1 and _segment_is_free(
            grid, centers[-2], goal, grid.navigable)):
        centers.pop()

    # Exact standoffs can intentionally sit inside the inflated halo (e.g. an
    # arm-reach pose beneath a counter overhang). Allow only the short endpoint
    # connector through uninflated floor, never through raw furniture/walls.
    connector_mask = grid.floor_mask & ~grid.raw_obstacle_mask
    if not _segment_is_free(grid, start, centers[0], connector_mask):
        raise NavigationPathError(
            f"start connector from {start.tolist()} crosses a static obstacle")
    if not _segment_is_free(grid, centers[-1], goal, connector_mask):
        raise NavigationPathError(
            f"goal connector to {goal.tolist()} crosses a static obstacle")

    route = [start.copy()]
    for point in centers:
        if np.linalg.norm(point - route[-1]) > 1e-6:
            route.append(point)
    if np.linalg.norm(goal - route[-1]) > 1e-6:
        route.append(goal.copy())
    else:
        route[-1] = goal.copy()
    return route


def _erode_for_transit(mask: np.ndarray, radius_cells: int) -> np.ndarray:
    """Inset free space for smoother transit without changing endpoint policy."""
    if radius_cells <= 0:
        return mask
    rows, cols = mask.shape
    padded = np.pad(mask, radius_cells, constant_values=False)
    out = np.ones_like(mask)
    for dr in range(-radius_cells, radius_cells + 1):
        for dc in range(-radius_cells, radius_cells + 1):
            if dr * dr + dc * dc > radius_cells * radius_cells:
                continue
            out &= padded[
                radius_cells + dr:radius_cells + dr + rows,
                radius_cells + dc:radius_cells + dc + cols,
            ]
    return out


def _route_score(route: list[np.ndarray]) -> tuple[int, int, float]:
    lengths = [
        float(np.linalg.norm(end - start))
        for start, end in zip(route, route[1:])
    ]
    return (
        sum(length + 1e-9 < MIN_ROUTE_SEGMENT for length in lengths),
        len(lengths),
        sum(lengths),
    )


def _simplify_exact_route(
    grid: NavigationGrid,
    route: list[np.ndarray],
) -> list[np.ndarray]:
    """Re-simplify a transit candidate against the original navigation mask."""
    if len(route) <= 2:
        return route
    last = len(route) - 1
    endpoint_free = []
    for point in route:
        cell = grid.cell_for_xy(point)
        endpoint_free.append(bool(cell is not None and grid.navigable[cell]))
    best: list[tuple[tuple[int, int, float], int] | None] = [None] * len(route)
    best[0] = ((0, 0, 0.0), -1)
    for end in range(1, len(route)):
        for start in range(end):
            previous = best[start]
            if previous is None:
                continue
            # A standoff may be in the inflated halo. Preserve its already
            # validated raw-free connector; never shortcut farther through it.
            connector = (
                (start == 0 and not endpoint_free[0] and end == 1)
                or (end == last and not endpoint_free[last] and start == last - 1)
            )
            if not connector and not _segment_is_free(
                    grid, route[start], route[end], grid.navigable):
                continue
            length = float(np.linalg.norm(route[end] - route[start]))
            old_score = previous[0]
            score = (
                old_score[0] + int(length + 1e-9 < MIN_ROUTE_SEGMENT),
                old_score[1] + 1,
                old_score[2] + length,
            )
            if best[end] is None or score < best[end][0]:
                best[end] = (score, start)
    if best[-1] is None:
        return route
    indices = [last]
    while indices[-1] != 0:
        indices.append(best[indices[-1]][1])
    return [route[index] for index in reversed(indices)]


def plan_grid_segment(grid: NavigationGrid, start, goal) -> list[np.ndarray]:
    """Plan a full route, preferring safe segments of at least 0.40 metres."""
    base_route = _plan_grid_segment_core(grid, start, goal)
    best_route = base_route
    best_score = _route_score(base_route)
    if best_score[0] == 0:
        return base_route

    max_cells = max(1, int(math.ceil(MAX_TRANSIT_CLEARANCE / grid.resolution)))
    # One alternate search at the configured maximum keeps compilation cheap;
    # if the inset disconnects a narrow corridor, retain the original safe path.
    for radius_cells in (max_cells,):
        transit_mask = _erode_for_transit(grid.navigable, radius_cells)
        try:
            candidate = _plan_grid_segment_core(
                replace(grid, navigable=transit_mask), start, goal)
        except NavigationPathError:
            continue
        candidate = _simplify_exact_route(grid, candidate)
        score = _route_score(candidate)
        if score < best_score:
            best_route, best_score = candidate, score
        if best_score[0] == 0:
            break
    return best_route


def plan_grid_route(
    grid: NavigationGrid,
    start,
    goal,
    via_points=None,
) -> list[np.ndarray]:
    """Plan through authored constraints and return one complete route.

    ``via_points`` are constraints. The returned route is derived output and
    always includes the exact start and final destination exactly once.
    """
    anchors = [np.asarray(start, dtype=float)[:2]]
    anchors.extend(np.asarray(point, dtype=float)[:2] for point in (via_points or []))
    anchors.append(np.asarray(goal, dtype=float)[:2])
    route = [anchors[0].copy()]
    for segment_start, segment_goal in zip(anchors, anchors[1:]):
        segment = plan_grid_segment(grid, segment_start, segment_goal)
        route.extend(point for point in segment[1:])
    return route
