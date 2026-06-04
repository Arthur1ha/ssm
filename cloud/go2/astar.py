import heapq
import math
import numpy as np
from typing import Optional


def astar(
    grid: np.ndarray,
    start: tuple[int, int],
    goal: tuple[int, int],
) -> Optional[list[tuple[int, int]]]:
    """
    grid: bool array shape (ny, nx), True = obstacle
    start/goal: (ix, iy)
    Returns [(ix, iy), ...] path, or None if unreachable.
    """
    if start == goal:
        return [start]

    ny, nx = grid.shape

    def in_bounds(ix, iy):
        return 0 <= ix < nx and 0 <= iy < ny

    def passable(ix, iy):
        return in_bounds(ix, iy) and not grid[iy, ix]

    def h(ix, iy):
        return math.sqrt((ix - goal[0]) ** 2 + (iy - goal[1]) ** 2)

    DIRS = [
        (1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
        (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414),
    ]

    open_heap: list = []
    heapq.heappush(open_heap, (h(*start), 0.0, start))
    came_from: dict[tuple, tuple] = {}
    g_score: dict[tuple, float] = {start: 0.0}

    while open_heap:
        _, g, current = heapq.heappop(open_heap)

        if current == goal:
            path = []
            while current in came_from:
                path.append(current)
                current = came_from[current]
            path.append(start)
            return list(reversed(path))

        if g > g_score.get(current, float("inf")):
            continue

        for dx, dy, cost in DIRS:
            nb = (current[0] + dx, current[1] + dy)
            if not passable(*nb):
                continue
            new_g = g_score[current] + cost
            if new_g < g_score.get(nb, float("inf")):
                g_score[nb] = new_g
                came_from[nb] = current
                heapq.heappush(open_heap, (new_g + h(*nb), new_g, nb))

    return None
