"""Choose scoped observation poses in the full measured reachable component."""
import heapq
import math
import numpy as np

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


def _map_bounds(grid):
    return (grid.origin[0], grid.origin[1],
            grid.origin[0] + grid.width[0] * grid.resolution,
            grid.origin[1] + grid.width[1] * grid.resolution)


def raycast_directions(grid_obj, odom: dict) -> dict[str, float]:
    """Diagnostic sector extents of the same full reachable component."""
    result = dict.fromkeys(DIRECTIONS, 0.0)
    for cell in _reachable(grid_obj, odom, _map_bounds(grid_obj)):
        x, y = grid_obj.grid_to_odom(*cell)
        dx, dy = x - odom['x'], y - odom['y']
        sector = round((math.atan2(dy, dx) - odom.get('heading', 0.0)) / (math.pi / 4)) % 8
        key = DIRECTIONS[sector]
        result[key] = max(result[key], math.hypot(dx, dy))
    return {key: round(value, 2) for key, value in result.items()}


def reachable_summary(grid_obj, odom: dict) -> dict[str, float | int]:
    cells = _reachable(grid_obj, odom, _map_bounds(grid_obj))
    distance = max((math.hypot(x - odom['x'], y - odom['y'])
                    for x, y in (grid_obj.grid_to_odom(*cell) for cell in cells)), default=0.0)
    return {'connected_cells': len(cells), 'max_displacement_m': round(distance, 2)}


# Full-map observation selection is an independent SSM implementation of
# reachable boundary clustering; no upstream runtime or copied source.
from dataclasses import asdict, dataclass
from cloud.go2.navigation.observation_coverage import ObservationCoverage
from cloud.go2.navigation.occupancy_grid import UNKNOWN
from cloud.go2.navigation.occupancy_grid import TURN_CLEARANCE_M


@dataclass(frozen=True)
class ObservationTarget:
    x: float
    y: float
    heading: float
    source: str
    score: float
    path_cost_m: float
    map_session: str | None

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Selection:
    target: ObservationTarget | None
    reason: str


def _can_start_observation(grid, odom, target, bounds, plans):
    """Preview Navigator's first action without acquiring control or moving."""
    from cloud.go2.navigation.navigator import (
        Navigator, _find_lookahead_on_path, _normalize_angle,
        ARRIVAL_THRESHOLD, HEADING_THRESHOLD, PATH_HEADING_INHIBIT,
        WALK_SPEED, MIN_LINEAR_VEL, TURN_SPEED, KP_YAW,
    )
    key = (target.x, target.y, target.heading)
    if key not in plans:
        plans[key] = Navigator()._plan_path(
            {'x': target.x, 'y': target.y, 'heading': target.heading, 'bounds': bounds}, grid, odom)
    planned = plans[key]
    if planned is None:
        return False
    path, route_grid, goal = planned
    distance = math.hypot(goal[0]-odom['x'], goal[1]-odom['y'])
    if distance < ARRIVAL_THRESHOLD:
        error = _normalize_angle(target.heading-odom['heading'])
        return (abs(error) <= HEADING_THRESHOLD
                or Navigator._turn_details(grid, odom, target.heading)['clear'])
    x, y = _find_lookahead_on_path(path, route_grid, odom['x'], odom['y'],
                                  odom['heading'], {'x': goal[0], 'y': goal[1]},
                                  front_only=True)
    error = _normalize_angle(math.atan2(y-odom['y'], x-odom['x'])-odom['heading'])
    if abs(error) > PATH_HEADING_INHIBIT:
        return Navigator._turn_details(grid, odom, odom['heading']+error)['clear']
    speed = min(WALK_SPEED, max(MIN_LINEAR_VEL, distance*.8))
    yaw = max(-TURN_SPEED, min(TURN_SPEED, KP_YAW*error))
    body_clear = getattr(grid, 'is_body_clear', None)
    return bool(callable(body_clear)
                and body_clear(odom['x'], odom['y'], odom['heading'])
                and body_clear(odom['x']+speed*math.cos(odom['heading'])*.3,
                               odom['y']+speed*math.sin(odom['heading'])*.3,
                               odom['heading']+yaw*.3))


def _forward_observation_target(grid, odom, coverage, bounds, session):
    """A measured forward step may expose floor needed for a later turn."""
    body_clear = getattr(grid, 'is_body_clear', None)
    sweep = getattr(grid, 'turn_sweep_details', None)
    if not callable(body_clear) or not callable(sweep):
        return None
    heading = odom['heading']
    # Include clearance beyond the endpoint; absent returns never grant passage.
    for distance in np.linspace(0., .60, 13):
        x = odom['x'] + distance*math.cos(heading)
        y = odom['y'] + distance*math.sin(heading)
        if not (bounds[0]+TURN_CLEARANCE_M <= x <= bounds[2]-TURN_CLEARANCE_M
                and bounds[1]+TURN_CLEARANCE_M <= y <= bounds[3]-TURN_CLEARANCE_M
                and body_clear(x, y, heading)
                and sweep(x, y, heading, heading)['clear']):
            return None
    target = ObservationTarget(odom['x']+.30*math.cos(heading),
                               odom['y']+.30*math.sin(heading), heading,
                               'observation_reposition', 0., .30, session)
    return target if coverage.failures(target) < 2 else None


def _reachable(grid, odom, bounds, *, clearance=0.0):
    try:
        start = grid.odom_to_grid(odom['x'], odom['y'])
    except (ValueError, KeyError, TypeError):
        return {}
    if not grid.is_free(*start):
        return {}
    def inside(cell):
        x, y = grid.grid_to_odom(*cell)
        return (bounds[0] + clearance <= x <= bounds[2] - clearance
                and bounds[1] + clearance <= y <= bounds[3] - clearance)
    if not inside(start):
        return {}
    costs = {start: 0.0}
    heap = [(0.0, start)]
    while heap:
        cost, (ix, iy) = heapq.heappop(heap)
        if cost != costs[(ix, iy)]:
            continue
        for dx, dy, weight in _NEIGHBORS:
            nb = ix + dx, iy + dy
            if not grid.is_free(*nb) or not inside(nb):
                continue
            if dx and dy and (not grid.is_free(ix + dx, iy) or not grid.is_free(ix, iy + dy)):
                continue
            value = cost + weight * grid.resolution
            if value < costs.get(nb, float('inf')):
                costs[nb] = value
                heapq.heappush(heap, (value, nb))
    return costs


def select_observation_target(grid, odom, coverage, *, bounds=None, current=None):
    """Select a reachable position and camera orientation from the live map.

    Dijkstra and boundary clusters consider the entire bounded map. A fixed
    representative budget bounds visual scoring, not destination/path distance.
    """
    if bounds is None:
        xmin, ymin = (float(value) for value in grid.origin[:2])
        bounds = (xmin, ymin, xmin + int(grid.width[0]) * float(grid.resolution),
                  ymin + int(grid.width[1]) * float(grid.resolution))
    if (len(bounds) != 4 or not all(math.isfinite(v) for v in bounds)
            or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
        return Selection(None, 'map_extent_invalid')
    session = getattr(grid, 'map_session', None)
    if session and coverage.map_session != session:
        return Selection(None, 'map_session_changed')
    # Keep the entire robot body inside the available map along the route.
    costs = _reachable(grid, odom, bounds, clearance=TURN_CLEARANCE_M)
    if not costs:
        return Selection(None, 'no_reachable_observation_point')
    # Unknown space near the room boundary can still be worth observing from
    # a safe stand-back pose. These cells supply information, never destinations.
    boundary_costs = _reachable(grid, odom, bounds)
    frontier = set()
    for ix, iy in boundary_costs:
        for dx, dy, _ in _NEIGHBORS[:4]:
            nx, ny = ix + dx, iy + dy
            if 0 <= nx < grid.width[0] and 0 <= ny < grid.width[1]:
                x, y = grid.grid_to_odom(nx, ny)
                if (bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]
                        and grid.cells[ny, nx] == UNKNOWN):
                    frontier.add((ix, iy))
                    break
    clusters = []
    remaining = set(frontier)
    while remaining:
        seed = min(remaining)
        remaining.remove(seed)
        cluster, queue = [seed], [seed]
        while queue:
            ix, iy = queue.pop()
            for dx, dy, _ in _NEIGHBORS:
                cell = ix + dx, iy + dy
                if cell in remaining:
                    remaining.remove(cell)
                    queue.append(cell)
                    cluster.append(cell)
        clusters.append(cluster)
    # Compute a conservative full-turn mask once instead of repeating thousands
    # of overlapping disk queries around sparse frontier clusters.
    turn_mask = None
    if hasattr(grid, 'known_floor') and hasattr(grid, 'obstacles'):
        blocked = grid.obstacles
        turn_mask = ~blocked.copy()
        ny, nx = blocked.shape
        reach = math.ceil((TURN_CLEARANCE_M + grid.resolution / 2) / grid.resolution)
        for dy in range(-reach, reach + 1):
            for dx in range(-reach, reach + 1):
                if math.hypot(dx, dy) * grid.resolution > TURN_CLEARANCE_M + grid.resolution / 2:
                    continue
                y0, y1 = max(0, -dy), min(ny, ny - dy)
                x0, x1 = max(0, -dx), min(nx, nx - dx)
                turn_mask[y0:y1, x0:x1] &= ~blocked[y0+dy:y1+dy, x0+dx:x1+dx]
        turn_mask[:reach, :] = turn_mask[-reach:, :] = False
        turn_mask[:, :reach] = turn_mask[:, -reach:] = False
    representatives = {}
    turn_clear = {}
    for cluster in sorted(clusters, key=len, reverse=True)[:32]:
        cx = sum(c[0] for c in cluster) / len(cluster)
        cy = sum(c[1] for c in cluster) / len(cluster)
        point = min(cluster, key=lambda c: ((c[0]-cx)**2 + (c[1]-cy)**2, boundary_costs[c], c))
        # Stand back from unknown ground so the complete body can occupy the
        # observation pose. This is a geometric check, not a distance cap from
        # the robot; a far boundary remains eligible.
        nearby = []
        radius = math.ceil(.8 / grid.resolution)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                cell = point[0] + dx, point[1] + dy
                if cell in costs:
                    nearby.append(((dx*dx + dy*dy), costs[cell], cell))
        for _, _, cell in sorted(nearby):
            wx, wy = grid.grid_to_odom(*cell)
            if cell not in turn_clear:
                check = getattr(grid, 'is_turn_clear', None)
                turn_clear[cell] = (bool(turn_mask[cell[1], cell[0]]) if turn_mask is not None
                                    else check is None or check(wx, wy))
            if turn_clear[cell]:
                representatives[cell] = ('frontier', min(len(cluster), 100))
                break
    # Contradictory evidence requests a view, never clearing blocked cells.
    pending = getattr(grid, 'pending_recheck', None)
    rechecks = []
    if pending is not None:
        pending_cells = list(zip(*np.nonzero(pending)))
        stride = max(1, math.ceil(len(pending_cells) / 16))
        for iy, ix in pending_cells[::stride][:16]:
            wx, wy = grid.grid_to_odom(int(ix), int(iy))
            if not (bounds[0] <= wx <= bounds[2] and bounds[1] <= wy <= bounds[3]):
                continue
            rechecks.append((int(ix), int(iy), wx, wy))
            options = []
            for sector in range(8):
                angle = sector * math.pi / 4
                try:
                    cell = grid.odom_to_grid(wx + math.cos(angle), wy + math.sin(angle))
                except ValueError:
                    continue
                if cell in costs:
                    options.append(cell)
            for cell in sorted(options, key=lambda c: (costs[c], c))[:2]:
                representatives[cell] = ('pending_recheck', 50)

    # Sample the full known component deterministically, including the current
    # pose. This also works when a completely known room has no frontier.
    spacing = max(1, round(.75 / grid.resolution))
    samples = [cell for cell in sorted(costs) if cell[0] % spacing == 0 and cell[1] % spacing == 0]
    if len(samples) > 32:
        samples = [samples[i * len(samples) // 32] for i in range(32)]
    for cell in samples:
        representatives.setdefault(cell, ('visual_coverage', 0))
    start = grid.odom_to_grid(odom['x'], odom['y'])
    representatives[start] = ('visual_coverage', 0)
    candidates = []
    def candidate(x, y, heading, source, information, cost):
        if not (bounds[0] + TURN_CLEARANCE_M <= x <= bounds[2] - TURN_CLEARANCE_M
                and bounds[1] + TURN_CLEARANCE_M <= y <= bounds[3] - TURN_CLEARANCE_M):
            return None
        pose = {'x': x, 'y': y, 'heading': heading}
        gain = coverage.gain(grid, pose)
        visits = coverage.visits(pose)
        information = 0 if visits else information
        if not gain and not information:
            return None
        # Frontier information is useful only when that direction faces unknown.
        score = gain * grid.resolution ** 2 + information * .01 - cost * .08 - visits * .25
        target = ObservationTarget(x, y, heading, source, score, cost, session)
        if coverage.failures(target) >= 2:
            return None
        return target
    for cell, (source, information) in representatives.items():
        x, y = grid.grid_to_odom(*cell)
        if cell == start:
            x, y = odom['x'], odom['y']
        for sector in range(8):
            heading = sector * math.pi / 4
            info = 0
            if source == 'frontier':
                for distance in range(1, math.ceil(1.0 / grid.resolution) + 1):
                    tx = cell[0] + round(math.cos(heading) * distance)
                    ty = cell[1] + round(math.sin(heading) * distance)
                    if not (0 <= tx < grid.width[0] and 0 <= ty < grid.width[1]):
                        break
                    if grid.cells[ty, tx] == UNKNOWN:
                        info = information
                        break
                    if not grid.is_free(tx, ty):
                        break
            if source == 'pending_recheck':
                for tx, ty, wx, wy in rechecks:
                    bearing = math.atan2(wy - y, wx - x)
                    if abs((bearing - heading + math.pi) % (2 * math.pi) - math.pi) > math.pi / 6:
                        continue
                    length = math.hypot(wx - x, wy - y)
                    if not .35 <= length <= 3.0:
                        continue
                    count = max(1, math.ceil(length / grid.resolution))
                    clear = True
                    for i in range(1, count):
                        try:
                            gx, gy = grid.odom_to_grid(x + (wx-x)*i/count, y + (wy-y)*i/count)
                        except ValueError:
                            clear = False
                            break
                        if (gx, gy) != (tx, ty) and grid.obstacles[gy, gx]:
                            clear = False
                            break
                    if clear:
                        info += information
            target = candidate(x, y, heading, source, info, costs[cell])
            if target is None:
                continue
            # An observation requires a confirmed full body footprint, even if
            # the robot will turn there after translating along its safe route.
            sweep = getattr(grid, 'turn_sweep_details', None)
            if callable(sweep):
                details = sweep(x, y, odom['heading'] if cell == start else heading, heading)
                if not details['clear']:
                    continue
            candidates.append(target)
    if not candidates:
        return Selection(None, 'coverage_exhausted' if coverage.records else 'observation_geometry_unconfirmed')
    plans = {}
    best = next((target for target in sorted(
        candidates, key=lambda c: (c.score, -c.path_cost_m, -c.x, -c.y, -c.heading), reverse=True)
        if _can_start_observation(grid, odom, target, bounds, plans)), None)
    if best is None:
        forward = _forward_observation_target(grid, odom, coverage, bounds, session)
        return Selection(forward, 'observation_reposition_selected' if forward else
                         'observation_start_unconfirmed')
    if current is not None and current.map_session == session and coverage.failures(current) < 2:
        try:
            cell = grid.odom_to_grid(current.x, current.y)
        except ValueError:
            cell = None
        if cell in costs:
            retained = candidate(current.x, current.y, current.heading, current.source, 0, costs[cell])
            sweep = getattr(grid, 'turn_sweep_details', None)
            retained_clear = (not callable(sweep) or sweep(
                current.x, current.y, odom['heading'] if cell == start else current.heading,
                current.heading)['clear'])
            if (retained and retained_clear and best.score <= retained.score * 1.25 + .25
                    and _can_start_observation(grid, odom, retained, bounds, plans)):
                return Selection(retained, 'target_retained')
    return Selection(best, {'frontier': 'frontier_selected', 'pending_recheck': 'recheck_selected'}.get(best.source, 'unobserved_view_selected'))
