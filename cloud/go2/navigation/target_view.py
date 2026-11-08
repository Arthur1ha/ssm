"""Bearing-only target memory and measured short viewpoint proposals.

No connection, vision model or motion dispatch lives in this module. An image
direction proposes viewpoints; it never establishes an object range or position.
"""
import math
from numbers import Real
import time

from cloud.go2.navigation.occupancy_grid import TURN_CLEARANCE_M

HINT_MAX_AGE_S = 60.
HINT_MAX_TRAVEL_M = 1.
VIEW_STEP_M = .30
APPROACH_STEP_M = .80


def _number(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _angle(value):
    return math.atan2(math.sin(value), math.cos(value))


def make_target_hint(sample):
    """Keep the actual capture pose and a nominal odom bearing, never depth."""
    if not isinstance(sample, dict) or sample.get('found') is not True:
        return None
    frame = sample.get('frame') or {}
    observation = sample.get('observation') or {}
    if not isinstance(frame, dict) or not isinstance(observation, dict):
        return None
    pose = frame.get('pose_at_capture') or {}
    if not isinstance(pose, dict):
        return None
    box = observation.get('bbox_1000')
    session = frame.get('odom_session_id')
    stamp = frame.get('received_at')
    if (not isinstance(session, str) or not session or pose.get('session_id') != session
            or not all(_number(pose.get(k)) for k in ('x', 'y', 'heading'))
            or not _number(stamp) or not isinstance(box, (list, tuple)) or len(box) != 4
            or not all(_number(v) for v in box)
            or not (0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000)
            or not all(_number(frame.get(k)) and frame[k] > 0 for k in ('width', 'height'))
            or not isinstance(frame.get('frame_id'), (str, int))):
        return None
    capture = {key: float(pose[key]) for key in ('x', 'y', 'heading')}
    capture['session_id'] = session
    if _number(pose.get('z')):
        capture['z'] = float(pose['z'])
    orientation = pose.get('orientation')
    if isinstance(orientation, (list, tuple)) and all(_number(v) for v in orientation):
        capture['orientation'] = [float(v) for v in orientation]
    offset = (box[0] + box[2] - 1000) / 1000
    return {'kind': 'bearing_only', 'capture_pose': capture,
            'odom_session_id': session, 'frame_id': frame['frame_id'],
            'received_at': float(stamp), 'bbox_1000': [float(v) for v in box],
            'width': int(frame['width']), 'height': int(frame['height']),
            'bearing_odom_rad': _angle(capture['heading']-math.atan(offset)),
            'close_in_image': bool(box[3]-box[1] >= 300 or box[3] >= 850)}


def target_hint_valid(hint, pose, *, now=None):
    if not isinstance(hint, dict) or hint.get('kind') != 'bearing_only' or not isinstance(pose, dict):
        return False
    capture = hint.get('capture_pose') or {}
    if not isinstance(capture, dict):
        return False
    stamp = hint.get('received_at')
    now = time.monotonic() if now is None else now
    return bool(_number(stamp) and 0 <= now-stamp <= HINT_MAX_AGE_S
        and _number(hint.get('bearing_odom_rad'))
        and isinstance(hint.get('odom_session_id'), str) and hint['odom_session_id']
        and hint['odom_session_id'] == pose.get('session_id') == capture.get('session_id')
        and all(_number(p.get(k)) for p in (capture, pose) for k in ('x', 'y', 'heading'))
        and math.hypot(pose['x']-capture['x'], pose['y']-capture['y']) <= HINT_MAX_TRAVEL_M)


def select_target_view(grid, pose, hint, bounds=None, attempted=(), *, approach=False):
    """Prefer an 80cm approach on clear paths; shorter views remain a fallback."""
    if not target_hint_valid(hint, pose):
        return None
    session = getattr(grid, 'map_session', getattr(grid, 'session_id', None))
    if session is not None and session != pose['session_id']:
        return None
    body = getattr(grid, 'is_body_clear', None)
    sweep = getattr(grid, 'turn_sweep_details', None)
    if not callable(body) or not callable(sweep):
        return None
    if bounds is not None and (len(bounds) != 4 or not all(_number(v) for v in bounds)
                              or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
        return None

    def clear(x, y, heading):
        return (bounds is None or
                bounds[0]+TURN_CLEARANCE_M <= x <= bounds[2]-TURN_CLEARANCE_M
                and bounds[1]+TURN_CLEARANCE_M <= y <= bounds[3]-TURN_CLEARANCE_M) \
            and body(x, y, heading) and sweep(x, y, heading, heading)['clear']

    def repeated(x, y, heading):
        return any(isinstance(old, dict) and all(_number(old.get(k)) for k in ('x', 'y', 'heading'))
                   and math.hypot(x-old['x'], y-old['y']) < .10
                   and abs(_angle(heading-old['heading'])) < .15 for old in attempted)

    h = pose['heading']
    delta = _angle(hint['bearing_odom_rad']-h)
    side = 1 if delta >= 0 else -1
    capture = hint['capture_pose']
    sideways = (-(pose['x']-capture['x'])*math.sin(capture['heading'])
                +(pose['y']-capture['y'])*math.cos(capture['heading']))
    # When the last sighting predates a lateral move, glance back across that
    # move. This is a bounded camera scan, not a triangulated object position.
    glance_back = abs(sideways) >= .10 and any(
        isinstance(old, dict) and old.get('view_kind') == 'lateral' for old in attempted)
    turns = ([max(-.45, min(.45, delta))] if abs(delta) >= .30 else
             [-math.copysign(.35, sideways)] if glance_back else [side*.35, -side*.35])
    if approach:
        # A coarse direction does not need repeated tiny alignment turns.
        # Do not oscillate left/right when a centred target has a blocked path.
        turns = ([max(-.75, min(.75, delta))] if abs(delta) > .35 else
                 [-math.copysign(.35, sideways)] if glance_back else [])

    def turn_candidate():
        for turn in turns:
            heading = _angle(h+turn)
            if (not repeated(pose['x'], pose['y'], heading)
                    and clear(pose['x'], pose['y'], h)
                    and clear(pose['x'], pose['y'], heading)
                    and sweep(pose['x'], pose['y'], h, heading)['clear']):
                return {'x': float(pose['x']), 'y': float(pose['y']), 'heading': heading,
                        'holonomic': True, 'source': 'target_reobservation', 'view_kind': 'turn',
                        'odom_session_id': pose['session_id'], 'target_distance_known': False}
        return None

    if abs(delta) > (.35 if approach else .60) or glance_back:
        turn = turn_candidate()
        if turn is not None:
            return turn
    directions = [('lateral', h+side*math.pi/2), ('lateral', h-side*math.pi/2)]
    if not hint.get('close_in_image') and abs(delta) < math.pi/3:
        directions.insert(0 if approach or abs(delta) < .20 else 1, ('forward', h))
    for kind, direction in directions:
        lengths = (APPROACH_STEP_M, .40) if approach and kind == 'forward' else (VIEW_STEP_M,)
        for length in lengths:
            x, y = pose['x']+length*math.cos(direction), pose['y']+length*math.sin(direction)
            if math.hypot(x-capture['x'], y-capture['y']) > HINT_MAX_TRAVEL_M or repeated(x, y, h):
                continue
            # Check the entire longer segment and its braking margin, not only
            # the endpoint; obstacles can shorten a step before it is dispatched.
            count = math.ceil((length + .10) / .05)
            if all(clear(pose['x']+d*math.cos(direction), pose['y']+d*math.sin(direction), h)
                   for d in (i * (length + .10) / count for i in range(count + 1))):
                return {'x': float(x), 'y': float(y), 'heading': float(h), 'holonomic': True,
                        'source': 'target_reobservation', 'view_kind': kind,
                        'odom_session_id': pose['session_id'], 'target_distance_known': False}
    # Turning has no translational parallax, but can recover a clipped view.
    return turn_candidate()
