"""Choose short exploration targets in measured, connected free space."""
import heapq
import math
from typing import Optional

_MAX_RAYCAST_DIST = 3.0  # maximum displacement within the local map
_TARGET_RATIO = 0.8       # keep a margin before the edge of known free space
_MIN_MOVE_DIST = 0.5
MIN_REACHABLE_DIST = _MIN_MOVE_DIST / _TARGET_RATIO
_MAX_PATH_DIST = 4.5  # keep each exploration step local even when the route turns

_DIRECTION_OFFSETS: dict[str, float] = {
    "forward": 0.0,
    "forward_left": math.pi / 4,
    "left": math.pi / 2,
    "backward_left": 3 * math.pi / 4,
    "backward": math.pi,
    "backward_right": -3 * math.pi / 4,
    "right": -math.pi / 2,
    "forward_right": -math.pi / 4,
}
DIRECTIONS = list(_DIRECTION_OFFSETS)
_NEIGHBORS = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
              (1, 1, math.sqrt(2)), (1, -1, math.sqrt(2)),
              (-1, 1, math.sqrt(2)), (-1, -1, math.sqrt(2)))


def _raycast(grid_obj, x: float, y: float, angle_rad: float) -> float:
    """Straight line diagnostic, retained for callers that need line of sight."""
    step = grid_obj.resolution
    steps = int(_MAX_RAYCAST_DIST / step)
    cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
    nx, ny = grid_obj.width[0], grid_obj.width[1]
    ox, oy = grid_obj.origin[0], grid_obj.origin[1]
    for i in range(1, steps + 1):
        wx, wy = x + cos_a * step * i, y + sin_a * step * i
        ix_raw = math.floor((wx - ox) / step)
        iy_raw = math.floor((wy - oy) / step)
        if not (0 <= ix_raw < nx and 0 <= iy_raw < ny):
            return step * (i - 1)
        if not grid_obj.is_free(ix_raw, iy_raw):
            return step * (i - 1)
    return _MAX_RAYCAST_DIST


def _reachable_candidates(grid_obj, odom: dict) -> dict[str, list[tuple[float, float, float]]]:
    """Search known free cells; sectors refer to destination bearing.

    This uses the same diagonal corner rule as A*. Unknown cells stay blocked.
    The path length limit keeps the selected route inside a short local step.
    """
    candidates = {direction: [] for direction in DIRECTIONS}
    try:
        start = grid_obj.odom_to_grid(odom["x"], odom["y"])
    except (KeyError, ValueError, TypeError):
        return candidates
    if not grid_obj.is_free(*start):
        return candidates

    resolution = grid_obj.resolution
    max_path = _MAX_PATH_DIST
    heap = [(0.0, start)]
    costs = {start: 0.0}
    while heap:
        cost, (ix, iy) = heapq.heappop(heap)
        if cost > costs[(ix, iy)] or cost > max_path:
            continue
        wx, wy = grid_obj.grid_to_odom(ix, iy)
        dx, dy = wx - odom["x"], wy - odom["y"]
        distance = math.hypot(dx, dy)
        if 0 < distance <= _MAX_RAYCAST_DIST:
            relative = (math.atan2(dy, dx) - odom.get("heading", 0.0)) % (2 * math.pi)
            sector = round(relative / (math.pi / 4)) % 8
            candidates[DIRECTIONS[sector]].append((distance, wx, wy))
        for sx, sy, weight in _NEIGHBORS:
            nb = (ix + sx, iy + sy)
            if not grid_obj.is_free(*nb):
                continue
            if sx and sy and (not grid_obj.is_free(ix + sx, iy)
                              or not grid_obj.is_free(ix, iy + sy)):
                continue
            next_cost = cost + weight * resolution
            if next_cost < costs.get(nb, float("inf")) and next_cost <= max_path:
                costs[nb] = next_cost
                heapq.heappush(heap, (next_cost, nb))
    return candidates


def raycast_directions(grid_obj, odom: dict) -> dict[str, float]:
    """Return maximum reachable displacement in each heading-relative sector.

    The historical name is kept for the exploration caller; paths may turn.
    """
    candidates = _reachable_candidates(grid_obj, odom)
    return {direction: round(max((p[0] for p in points), default=0.0), 2)
            if _target_in_sector(points) else 0.0
            for direction, points in candidates.items()}


def reachable_summary(grid_obj, odom: dict) -> dict[str, float | int]:
    """Report the connected green region, including distances too short to move."""
    candidates = _reachable_candidates(grid_obj, odom)
    distances = [p[0] for points in candidates.values() for p in points]
    return {"connected_cells": len(distances) + 1,
            "max_displacement_m": round(max(distances, default=0.0), 2)}


def _target_in_sector(points: list[tuple[float, float, float]]) -> Optional[tuple[float, float]]:
    if not points:
        return None
    limit = max(p[0] for p in points) * _TARGET_RATIO
    suitable = [p for p in points if _MIN_MOVE_DIST <= p[0] <= limit]
    if suitable:
        _, x, y = max(suitable, key=lambda p: p[0])
        return x, y
    return None


def find_exploration_target(
    grid_obj,
    odom: dict,
    preferred_direction: Optional[str] = None,
) -> Optional[tuple[float, float]]:
    """Pick a reachable known-free cell at least 0.5m away from the robot."""
    candidates = _reachable_candidates(grid_obj, odom)
    if preferred_direction in candidates:
        directions = [preferred_direction]
    else:
        directions = sorted(DIRECTIONS,
                            key=lambda d: max((p[0] for p in candidates[d]), default=0.0),
                            reverse=True)
    for direction in directions:
        target = _target_in_sector(candidates[direction])
        if target:
            return target
    return None


def find_frontier_target(
    grid_obj,
    odom: dict,
    visited: list[tuple[float, float]] | None = None,
) -> Optional[tuple[float, float]]:
    """Choose a reachable free cell near the boundary of measured space.

    The target is selected from map cells only. Camera observations never
    authorize a movement or choose a direction.
    """
    from cloud.go2.navigation.occupancy_grid import UNKNOWN

    cells = grid_obj.cells
    nx, ny = grid_obj.width[:2]
    visited = visited or []
    turn_check = getattr(grid_obj, "is_turn_clear", None)
    can_turn = turn_check(odom["x"], odom["y"]) if callable(turn_check) else True
    options = []
    forward_options = []
    for points in _reachable_candidates(grid_obj, odom).values():
        for distance, x, y in points:
            if distance < _MIN_MOVE_DIST or distance > 1.5:
                continue
            if any(math.hypot(x - px, y - py) < 0.4 for px, py in visited):
                continue
            ix, iy = grid_obj.odom_to_grid(x, y)
            unknown = 0
            for sy in range(max(0, iy - 3), min(ny, iy + 4)):
                for sx in range(max(0, ix - 3), min(nx, ix + 4)):
                    unknown += cells[sy, sx] == UNKNOWN
            if unknown:
                bearing = math.atan2(y - odom["y"], x - odom["x"])
                relative = (bearing - odom.get("heading", 0.0) + math.pi) % (2 * math.pi) - math.pi
                if not can_turn and abs(relative) > 0.45:
                    continue
                option = (unknown, -distance, x, y)
                options.append(option)
                if abs(relative) <= math.pi / 3:
                    forward_options.append(option)
    if not options:
        return None
    # A forward frontier needs less turning before the next live map frame.
    # If none exists, side and rear frontiers remain available for exploration.
    _, _, x, y = max(forward_options or options)
    return x, y
