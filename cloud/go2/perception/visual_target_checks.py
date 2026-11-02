import math
import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

from cloud.go2.perception.visual_target import (
    TargetUnlocalized, load_calibration, project_ground_contact,
    require_avoidance_evidence, select_standoff,
)


class Map:
    frame_id = 'odom'
    resolution = 0.1

    def __init__(self):
        self.grid = np.zeros((60, 60), dtype=bool)

    def odom_to_grid(self, x, y):
        gx, gy = round((x + 3) * 10), round((y + 3) * 10)
        if not (0 <= gx < 60 and 0 <= gy < 60):
            raise ValueError('outside map')
        return gx, gy

    def grid_to_odom(self, x, y):
        return x / 10 - 3, y / 10 - 3

    def is_free(self, x, y):
        return 0 <= x < 60 and 0 <= y < 60 and not self.grid[y, x]

    def is_body_clear(self, x, y, heading):
        return self.is_free(*self.odom_to_grid(x, y))


def target():
    frame = {'width': 640, 'height': 480, 'frame_id': 4, 'captured_at': time.time()}
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 'session'}
    calibration = {'cx': 320, 'cy': 240, 'fx': 400, 'fy': 400,
                   'rotation_body_from_camera': [[0, 0, 1], [-1, 0, 0], [0, -1, 0]],
                   'translation_body_from_camera_m': [0, 0, 0],
                   'camera_height_above_floor_m': .3,
                   'position_error_m': .05, 'reprojection_error_px': 1}
    observation = {'count': 1, 'confidence': .9, 'static': True,
                   'ground_contact_visible': True, 'contact_pixel': [320, 360]}
    return project_ground_contact(observation, frame, pose, 0., calibration), pose


def test_projection_and_standoff_distance():
    location, pose = target()
    assert math.isclose(location['x'], 1.)
    grid = Map()
    stop = select_standoff(grid, pose, location)
    assert 1.4 <= math.hypot(stop['x'] - location['x'], stop['y'] - location['y']) <= 1.7


def test_navigator_route_avoids_target_keepout():
    from cloud.go2.navigation.navigator import Navigator
    location, pose = target()
    grid = Map()
    goal = {'x': 2., 'y': 0., 'keepout': (location['x'], location['y'], 0.9)}
    planned = Navigator()._plan_path(goal, grid, pose)
    assert planned is not None
    path, _, _ = planned
    assert all(math.hypot(grid.grid_to_odom(x, y)[0] - location['x'],
                          grid.grid_to_odom(x, y)[1] - location['y']) >= 0.9
               for x, y in path)


def test_missing_site_evidence_blocks_runtime(tmp_path):
    with pytest.raises(TargetUnlocalized, match='calibration_unavailable'):
        load_calibration(tmp_path / 'camera_calibration.json')
    with pytest.raises(TargetUnlocalized, match='avoidance_unverified'):
        require_avoidance_evidence('robot-serial', tmp_path / 'avoidance_evidence.json')


def test_calibration_for_another_robot_is_rejected(tmp_path):
    path = tmp_path / 'camera_calibration.json'
    path.write_text(json.dumps({
        'robot_serial': 'other-robot', 'measured_on_robot': True,
        'replay_verified': True, 'camera_id': 'front',
        'odom_session_independent': True, 'posture': 'standing',
        'cx': 320, 'cy': 240, 'fx': 400, 'fy': 400,
        'reprojection_error_px': 1, 'position_error_m': .05,
        'camera_height_above_floor_m': .3,
        'rotation_body_from_camera': [[0, 0, 1], [-1, 0, 0], [0, -1, 0]],
        'translation_body_from_camera_m': [0, 0, 0],
    }))
    with pytest.raises(TargetUnlocalized, match='calibration_unavailable'):
        load_calibration(path, serial='current-robot')


@pytest.mark.asyncio
async def test_avoidance_diagnostic_only_queries_switch():
    from cloud.go2.connection.webrtc import Go2Connection
    from unitree_webrtc_connect.constants import OBSTACLES_AVOID_API, RTC_TOPIC

    calls = []

    async def request(topic, options):
        calls.append((topic, options))
        return {'data': {'header': {'status': {'code': 0}},
                         'data': '{"enable":true}'}}

    connection = Go2Connection()
    connection.is_connected = True
    connection._conn = SimpleNamespace(
        datachannel=SimpleNamespace(pub_sub=SimpleNamespace(publish_request_new=request)))
    result = await connection.read_avoidance_switch()
    assert calls == [(RTC_TOPIC['OBSTACLES_AVOID'],
                      {'api_id': OBSTACLES_AVOID_API['SWITCH_GET']})]
    assert result['enabled'] is True


def test_unknown_contact_and_out_of_map_fail_closed():
    location, pose = target()
    grid = Map()
    gx, gy = grid.odom_to_grid(location['x'], location['y'])
    for y in range(gy - 2, gy + 3):
        for x in range(gx - 2, gx + 3):
            grid.grid[y, x] = True
    with pytest.raises(TargetUnlocalized, match='target_ground_unverified'):
        select_standoff(grid, pose, location)
    with pytest.raises(TargetUnlocalized, match='target_outside_local_map'):
        select_standoff(Map(), pose, {**location, 'x': 9})


def test_ambiguous_or_bad_pixel_never_localizes():
    location, pose = target()
    frame = {'width': 640, 'height': 480, 'frame_id': 4, 'captured_at': time.time()}
    calibration = {'cx': 320, 'cy': 240, 'fx': 400, 'fy': 400,
                   'rotation_body_from_camera': [[0, 0, 1], [-1, 0, 0], [0, -1, 0]],
                   'translation_body_from_camera_m': [0, 0, 0],
                   'camera_height_above_floor_m': .3,
                   'position_error_m': .05, 'reprojection_error_px': 1}
    for observation in ({'count': 2, 'confidence': .9, 'static': True,
                         'ground_contact_visible': True, 'contact_pixel': [320, 360]},
                        {'count': 1, 'confidence': .9, 'static': True,
                         'ground_contact_visible': True, 'contact_pixel': None}):
        with pytest.raises(TargetUnlocalized):
            project_ground_contact(observation, frame, pose, 0., calibration)


@pytest.mark.asyncio
async def test_blocked_visual_tool_ends_graph_before_fallback_move(tmp_path):
    from cloud.agents.common.runner import GraphRun, run_role_graph
    from cloud.agents.common.checkpoint import FileCheckpointSaver
    from cloud.agents.common.effects import FileEffectLedger

    calls = []

    class Model:
        def bind_tools(self, schemas, parallel_tool_calls=False):
            return self

        async def ainvoke(self, messages):
            raise AssertionError('model must not plan a fallback after visual block')

    async def execute(name, args, step_id):
        calls.append(name)
        return {'result': 'blocked', 'reason': 'calibration_unavailable'}

    run = GraphRun('visual-run', 'visual-session', '走到画面中的猫头鹰旁边', time.time() + 5)
    run.agent_id = 'go2'
    await run_role_graph(
        run, model=Model(),
        tool_schemas={'go2_navigate_to_visible_target': {'name': 'go2_navigate_to_visible_target',
                      'description': 'visual', 'parameters': {'type': 'object', 'properties': {}}}},
        build_messages=lambda current: [], execute=execute,
        initial_tool=('go2_navigate_to_visible_target', {'target': '猫头鹰'}),
        terminal_tools=frozenset({'go2_navigate_to_visible_target'}),
        checkpointer=FileCheckpointSaver(tmp_path / 'checkpoint'),
        effect_ledger=FileEffectLedger(tmp_path / 'effects'),
    )
    assert calls == ['go2_navigate_to_visible_target']
    assert run.status == 'blocked'
