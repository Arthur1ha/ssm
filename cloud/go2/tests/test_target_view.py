"""Pure geometry tests: no connections, model queries or motion dispatch."""
import json
import math
import time
from types import SimpleNamespace

import pytest

from cloud.go2.navigation.target_view import make_target_hint, select_target_view, target_hint_valid


def sample(box=None):
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 'map'}
    return {'found': True, 'frame': {'pose_at_capture': pose, 'odom_session_id': 'map',
            'frame_id': 353, 'received_at': time.monotonic(), 'width': 640, 'height': 360},
            'observation': {'bbox_1000': box or [795, 520, 825, 580]}}


def grid(clear=lambda x, y, h: True):
    return SimpleNamespace(map_session='map', is_body_clear=clear,
        turn_sweep_details=lambda x, y, first, last: {'clear': clear(x, y, last)})


def test_hint_retains_capture_and_only_nominal_bearing():
    observed = sample()
    hint = make_target_hint(observed)
    assert json.loads(json.dumps(hint)) == hint
    assert hint['capture_pose'] == observed['frame']['pose_at_capture']
    assert hint['frame_id'] == 353 and hint['bbox_1000'] == [795, 520, 825, 580]
    assert hint['bearing_odom_rad'] < 0
    assert 'x' not in hint and 'y' not in hint and 'distance_m' not in hint
    assert make_target_hint({'found': False}) is None


@pytest.mark.parametrize('change', ['expired', 'future', 'session', 'far'])
def test_old_or_displaced_hint_never_authorizes_viewpoint(change):
    hint = make_target_hint(sample())
    pose = hint['capture_pose'].copy()
    if change == 'expired': hint['received_at'] -= 61
    if change == 'future': hint['received_at'] += 10
    if change == 'session': pose['session_id'] = 'new'
    if change == 'far': pose['x'] = 1.01
    assert not target_hint_valid(hint, pose)
    assert select_target_view(grid(), pose, hint) is None


def test_target_side_has_priority_and_attempted_view_is_not_repeated():
    hint = make_target_hint(sample())
    pose = hint['capture_pose']
    first = select_target_view(grid(), pose, hint)
    assert first['view_kind'] == 'lateral' and first['y'] < -.29
    assert math.hypot(first['x'], first['y']) <= .30
    assert first['heading'] == 0 and first['holonomic']
    second = select_target_view(grid(), pose, hint, attempted=[first])
    assert math.hypot(first['x']-second['x'], first['y']-second['y']) >= .10


def test_large_close_box_never_selects_forward_motion():
    hint = make_target_hint(sample([400, 500, 600, 900]))
    # Translation is available only in front; safely rotating remains possible.
    result = select_target_view(grid(lambda x,y,h: abs(y) < .01), hint['capture_pose'], hint)
    assert result['view_kind'] == 'turn'
    assert result['x'] == result['y'] == 0


def test_blocked_body_footprint_and_obstacles_remain_blocking():
    hint = make_target_hint(sample())
    g = grid()
    g.turn_sweep_details = lambda *args: {'clear': False, 'blockers': ['measured_obstacle']}
    assert select_target_view(g, hint['capture_pose'], hint) is None
    assert select_target_view(grid(lambda *args: False), hint['capture_pose'], hint) is None


def test_table_leg_on_target_side_chooses_another_clear_view():
    hint = make_target_hint(sample())
    result = select_target_view(grid(lambda x,y,h: y > -.1), hint['capture_pose'], hint)
    assert result and result['y'] >= -.1
    assert result['source'] == 'target_reobservation'


def test_scope_contains_entire_body_and_sweep():
    hint = make_target_hint(sample())
    assert select_target_view(grid(), hint['capture_pose'], hint, bounds=(-.2,-.2,.2,.2)) is None


def test_after_right_step_old_sighting_prompts_left_glance():
    hint = make_target_hint(sample([450, 500, 550, 600]))
    pose = {**hint['capture_pose'], 'y': -.30}
    attempted = [{'x': 0., 'y': -.30, 'heading': 0., 'view_kind': 'lateral'}]
    point = select_target_view(grid(), pose, hint, attempted=attempted)
    assert point['view_kind'] == 'turn' and point['heading'] > .30
    assert point['x'] == pose['x'] and point['y'] == pose['y']


def test_centered_target_uses_safe_forward_view_before_lateral():
    hint = make_target_hint(sample([450, 500, 550, 600]))
    point = select_target_view(grid(), hint['capture_pose'], hint)
    assert point['view_kind'] == 'forward'


def test_view_endpoint_stays_within_capture_hint_validity_radius():
    hint = make_target_hint(sample([450, 500, 550, 600]))
    point = select_target_view(grid(), {**hint['capture_pose'], 'x': 1.}, hint)
    assert point['view_kind'] == 'turn'
    assert math.hypot(point['x'], point['y']) <= 1.


def test_approximate_approach_faces_target_then_advances_without_depth():
    hint = make_target_hint(sample())
    point = select_target_view(grid(), hint['capture_pose'], hint, approach=True)
    assert point['view_kind'] == 'turn' and point['heading'] < 0
    centered = make_target_hint(sample([450, 500, 550, 600]))
    point = select_target_view(grid(), centered['capture_pose'], centered, approach=True)
    assert point['view_kind'] == 'forward' and point['x'] == pytest.approx(.80)
    assert point['target_distance_known'] is False


def test_approach_uses_long_step_despite_small_direction_error():
    hint = make_target_hint(sample([590, 500, 650, 600]))
    point = select_target_view(grid(), hint['capture_pose'], hint, approach=True)
    assert point['view_kind'] == 'forward' and point['x'] == pytest.approx(.80)


def test_obstacle_partway_through_long_step_shortens_the_move():
    hint = make_target_hint(sample([450, 500, 550, 600]))
    point = select_target_view(grid(lambda x,y,h: x < .6), hint['capture_pose'], hint, approach=True)
    assert point['view_kind'] == 'forward' and point['x'] == pytest.approx(.40)


def test_blocked_centered_target_does_not_trigger_pointless_alignment_turns():
    hint = make_target_hint(sample([450, 500, 550, 600]))
    point = select_target_view(grid(lambda x,y,h: math.hypot(x,y) < .05),
                               hint['capture_pose'], hint, approach=True)
    assert point is None
