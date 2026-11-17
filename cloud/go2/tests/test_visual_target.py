import asyncio
import math
import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

from cloud.go2.perception.visual_target import (
    TargetUnlocalized, detect_ground_target, detect_visible_target,
    load_calibration, project_ground_contact,
    select_standoff,
)


class Map:
    frame_id = 'odom'
    resolution = 0.1

    def __init__(self):
        self.grid = np.zeros((60, 60), dtype=bool)
        self.measured_floor = np.ones((60, 60), dtype=bool)

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


def test_missing_calibration_blocks_precise_navigation(tmp_path):
    with pytest.raises(TargetUnlocalized, match='calibration_unavailable'):
        load_calibration(tmp_path / 'camera_calibration.json')


@pytest.mark.asyncio
async def test_detection_requests_pixel_bbox_as_json(monkeypatch):
    from cloud.go2 import models
    from cloud.go2.perception import visual_target
    events = []

    class VisionModel:
        def bind(self, **kwargs):
            assert kwargs == {'response_format': {'type': 'json_object'}}
            return self

        async def ainvoke(self, messages):
            assert '640x480' in messages[0].content
            assert '绝对像素坐标' in messages[0].content
            return SimpleNamespace(content=json.dumps({
                'count': 1, 'confidence': 0.95, 'static': True,
                'bbox': [200, 100, 400, 300],
            }))

    monkeypatch.setattr(models, 'get_vision_llm', lambda: VisionModel())
    monkeypatch.setattr(visual_target, 'emit_agent_event',
                        lambda event, **fields: events.append((event, fields)))
    result = await detect_ground_target(b'jpeg', '白色毛绒玩具', {'width': 640, 'height': 480})
    assert result['bbox'] == [200, 100, 400, 300]
    assert any(event == 'vision.model' and fields.get('phase') == 'response'
               and '"confidence": 0.95' in fields['output']
               and fields['duration_ms'] >= 0 for event, fields in events)
    assert any(event == 'vision.model' and fields.get('phase') == 'parsed'
               and fields['observation']['bbox'] == [200, 100, 400, 300]
               for event, fields in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['cargo_geometry', 'cargo_delivery_geometry'])
@pytest.mark.parametrize('wrapped', [False, True])
async def test_cargo_geometry_logs_actual_fields_instead_of_empty_ground_target_fields(monkeypatch, mode, wrapped):
    from cloud.go2 import models
    from cloud.go2.perception import visual_target
    events = []
    observation = {'cargo_count': 1, 'cargo_ground_contact_visible': True,
                   'cargo_bbox_1000': [562, 709, 738, 835]}
    if mode == 'cargo_delivery_geometry':
        observation.update(destination_count=1, destination_ground_contact_visible=True,
                           destination_kind='marker', destination_bbox_1000=[605, 612, 625, 645])
    class VisionModel:
        def bind(self, **kwargs):
            return self

        async def ainvoke(self, messages):
            return SimpleNamespace(content=json.dumps([observation] if wrapped else observation))

    monkeypatch.setattr(models, 'get_vision_llm', lambda: VisionModel())
    monkeypatch.setattr(visual_target, 'emit_agent_event',
                        lambda event, **fields: events.append((event, fields)))
    assert await visual_target._ask_vision(b'jpeg', '盒子',
        {'width': 640, 'height': 360, 'frame_id': 1165}, '定位货物', mode) == observation
    parsed = next(fields['observation'] for event, fields in events
                  if event == 'vision.model' and fields.get('phase') == 'parsed')
    assert {key: parsed[key] for key in observation} == observation
    assert 'contact_pixel' not in parsed


@pytest.mark.asyncio
@pytest.mark.parametrize('records', [[], [{}, {}], ['not an observation']])
async def test_cargo_geometry_does_not_select_from_ambiguous_or_invalid_records(monkeypatch, records):
    from unittest.mock import AsyncMock
    from cloud.go2 import models
    from cloud.go2.perception import visual_target
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(records))))
    model.bind = lambda **_: model
    monkeypatch.setattr(models, 'get_vision_llm', lambda: model)
    with pytest.raises(TargetUnlocalized, match='vision_result_invalid'):
        await visual_target._ask_vision(b'jpeg', '盒子',
            {'width': 640, 'height': 360}, '定位货物', 'cargo_geometry')


@pytest.mark.asyncio
async def test_invalid_vision_json_logs_preview_and_blocks(monkeypatch):
    from cloud.go2 import models
    from cloud.go2.perception import visual_target
    events = []

    class VisionModel:
        def bind(self, **kwargs):
            return self

        async def ainvoke(self, messages):
            return SimpleNamespace(content='```json\n{"bbox":[1,2,3,4]}\n```')

    monkeypatch.setattr(models, 'get_vision_llm', lambda: VisionModel())
    monkeypatch.setattr(visual_target, 'emit_agent_event',
                        lambda event, **fields: events.append((event, fields)))
    with pytest.raises(TargetUnlocalized, match='vision_result_invalid'):
        await detect_ground_target(b'jpeg', '玩具', {'width': 640, 'height': 480})
    assert any(event == 'vision.model' and fields.get('phase') == 'response'
               and '```json' in fields['output'] for event, fields in events)
    assert any(event == 'vision.gate' and fields.get('gate') == 'json_response'
               and fields['passed'] is False for event, fields in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('feedback', ['standing', 'still_moving', 'stale_standing', 'disconnected', 'new_session', 'expired'])
async def test_posture_wait_requires_fresh_live_feedback_without_assigning_a_pose(monkeypatch, feedback):
    from cloud.go2.perception import visual_target
    clock = [100.]
    robot = SimpleNamespace(body_state='moving', is_connected=True,
                            _sport_state_observed_at=100., _odom_session_id='map')
    context = SimpleNamespace(expires_at=None)
    monkeypatch.setattr(visual_target, 'go2', robot)
    monkeypatch.setattr(visual_target, 'current_context', lambda: context)
    monkeypatch.setattr(visual_target.time, 'monotonic', lambda: clock[0])
    async def feedback_tick(_delay):
        clock[0] += 1.
        if feedback == 'standing':
            robot.body_state, robot._sport_state_observed_at = 'standing', clock[0]
        elif feedback == 'stale_standing':
            robot.body_state, robot._sport_state_observed_at = 'standing', 90.
        elif feedback == 'disconnected':
            robot.is_connected = False
        elif feedback == 'new_session':
            robot._odom_session_id = 'other'
        elif feedback == 'expired':
            context.expires_at = time.time() - 1
    monkeypatch.setattr(visual_target.asyncio, 'sleep', feedback_tick)
    if feedback == 'standing':
        await visual_target.wait_for_standing()
    else:
        reason = {'disconnected': 'disconnected', 'new_session': 'odom_session_mismatch',
                  'expired': 'authorization_expired'}.get(feedback, 'posture_not_confirmed')
        with pytest.raises(TargetUnlocalized, match=reason):
            await visual_target.wait_for_standing()
    if feedback == 'still_moving':
        assert robot.body_state == 'moving'


@pytest.mark.asyncio
async def test_missing_calibration_uses_lidar_without_claiming_a_target(monkeypatch):
    from unittest.mock import AsyncMock
    from cloud.go2.tools import tools
    from cloud.go2.perception import visual_target
    from cloud.go2.navigation import target_approach

    monkeypatch.setattr(tools.go2, 'is_connected', True)
    monkeypatch.setattr(tools.go2, 'body_state', 'standing')
    monkeypatch.setattr(tools.go2, '_connected_serial', 'robot-serial', raising=False)

    def missing_calibration(*, serial):
        raise TargetUnlocalized('calibration_unavailable')

    monkeypatch.setattr(visual_target, 'load_calibration', missing_calibration)
    locate = AsyncMock(side_effect=TargetUnlocalized('object_returns_unavailable'))
    monkeypatch.setattr(visual_target, 'locate_lidar_target', locate)
    result = await target_approach.navigate_to_visible_target('白色猫头鹰', allow_reobservation=False)
    assert result['result'] == 'blocked'
    assert result['reason'] == 'object_returns_unavailable'
    locate.assert_awaited_once()
    assert result['last_trusted_pose'] is None


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
            grid.measured_floor[y, x] = False
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


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_result', [
    {'result': 'blocked', 'reason': 'no_known_free_forward_path',
     'message': '目标方向没有已知可通行区域；本次未确认前进。'},
    {'result': 'ok', 'approached_m': 0.4,
     'message': '已朝白色猫头鹰方向前进约 0.4 米；尚未确认到达目标。'},
])
async def test_visual_result_preserves_arrival_boundary_in_user_reply(tmp_path, monkeypatch, tool_result):
    from cloud.agents.go2 import graph
    from cloud.agents.go2.run_view import Go2RunView
    from cloud.agents.common.checkpoint import FileCheckpointSaver
    from cloud.agents.common.run_index import FileRunIndex
    from cloud.space.execution import ExecutionContext

    class ResultModel:
        calls = 0

        def bind_tools(self, schemas, parallel_tool_calls=False):
            return self

        async def ainvoke(self, messages):
            from langchain_core.messages import AIMessage
            self.calls += 1
            # One original decision/closing response may describe the receipt;
            # no additional tool is needed to preserve its arrival boundary.
            return AIMessage(content=json.dumps({'kind': 'final', 'text': tool_result['message']}))

    async def execute_visual(name, params, *, context):
        assert name == 'go2_navigate_to_visible_target'
        return tool_result

    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    monkeypatch.setattr(graph, 'get_store', lambda: Go2RunView(
        checkpointer=FileCheckpointSaver(tmp_path / 'checkpoints'),
        index=FileRunIndex(tmp_path / 'index')))
    model = ResultModel()
    monkeypatch.setattr(graph, 'get_text_llm', lambda: model)
    monkeypatch.setattr(graph, 'execute_tool', execute_visual)
    monkeypatch.setattr(graph.go2, 'is_connected', True)
    monkeypatch.setattr('cloud.api.intents.publish_agent_event', lambda *_a, **_k: {})
    context = ExecutionContext('visual-reply-session', 'admin', True,
                               expires_at=int(time.time() + 60))
    result = await graph.run_go2_loop(
        'visual-reply-session', '走到白色猫头鹰旁边', context=context,
        skill_id='go2_visual_navigate', params={'target': '白色猫头鹰'},
        run_id='visual-reply-run')
    assert result['result'] == tool_result['result']
    assert result['response'] == tool_result['message']
    assert model.calls == 1
    if tool_result['result'] == 'ok':
        assert '尚未确认到达' in result['response']
    assert result['actions_taken'] == (['go2_navigate_to_visible_target']
                                       if tool_result['result'] == 'ok' else [])


@pytest.mark.asyncio
@pytest.mark.parametrize('persist', [True, False])
async def test_observation_keeps_authorized_frame_before_pose_rejection(monkeypatch, persist):
    from unittest.mock import AsyncMock
    from cloud.go2.perception import visual_target
    calls = []
    frame = {'frame_id': 849, 'odom_session_id': 'map',
             'pose_at_capture': {'x': 0., 'y': 0., 'heading': 0.}}
    monkeypatch.setattr(visual_target, '_capture_still', AsyncMock(return_value=(b'jpeg', frame, 1.)))
    monkeypatch.setattr(visual_target, 'current_context', lambda: SimpleNamespace(persist=persist))
    monkeypatch.setattr(visual_target, 'save_diagnostic_frame', lambda *args: calls.append('save'))
    async def detect(*args):
        calls.append('model')
        return {'found': False}
    monkeypatch.setattr(visual_target, 'detect_visible_target', detect)
    monkeypatch.setattr(visual_target, '_live_pose', lambda _: {'x': 1., 'y': 0., 'heading': 0.})
    with pytest.raises(TargetUnlocalized, match='pose_changed_during_detection'):
        await visual_target.observe_target('owl')
    assert calls == (['save', 'model'] if persist else ['model'])


@pytest.fixture
def lidar_localizer(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from cloud.go2.perception import visual_target, rgb_lidar
    from cloud.go2.navigation import occupancy_grid
    now = time.monotonic()
    pose = {'x': 0., 'y': 0., 'z': .4, 'heading': 0.,
            'orientation': [0, 0, 0, 1], 'session_id': 's'}
    frame = {'width': 640, 'height': 360, 'frame_id': 3, 'received_at': now,
             'pose_received_at': now, 'odom_session_id': 's', 'pose_at_capture': pose}
    voxel = {'data': {'resolution': .05, 'data': {'points': [[2., 0., .2]]}}}
    robot = SimpleNamespace(body_state='standing', _sport_state_observed_at=now,
                            voxel_raw=voxel, voxel_received_at=now,
                            odom_frame_id='odom', voxel_frame_id='odom')
    calls = []
    monkeypatch.setattr(visual_target, 'go2', robot)
    monkeypatch.setattr(visual_target, '_capture_still', AsyncMock(return_value=(b'jpeg', frame, 1.)))
    monkeypatch.setattr(visual_target, '_live_pose', lambda _: pose)
    monkeypatch.setattr(visual_target, 'current_context', lambda: SimpleNamespace(persist=True, expires_at=None))
    def save(*args):
        calls.append('image')
        return tmp_path / 'frame.jpg'
    monkeypatch.setattr(visual_target, 'save_diagnostic_frame', save)
    monkeypatch.setattr(rgb_lidar, 'save_paired_sample', lambda *a: calls.append('pair'))
    monkeypatch.setattr(occupancy_grid, 'OccupancyGrid', lambda *a, **k: SimpleNamespace(measured_floor_z=0.))
    async def detect(*args):
        calls.append('model')
        robot.voxel_raw = {'replacement': True}
        robot.voxel_received_at = now + 10
        return {'found': True, 'bbox_1000': [450, 450, 550, 850]}
    monkeypatch.setattr(visual_target, 'detect_visible_target', detect)
    def associate(paired, *args, **kwargs):
        assert paired is voxel
        calls.append('cluster')
        return {'x': 2., 'y': 0., 'uncertainty_m': .25, 'lidar_points': 4}
    monkeypatch.setattr(rgb_lidar, 'associate_lidar_target', associate)
    return visual_target, robot, pose, frame, calls


@pytest.mark.asyncio
async def test_lidar_localization_preserves_pre_model_pair_and_source(lidar_localizer):
    module, robot, pose, frame, calls = lidar_localizer
    location, current, grid = await module.locate_lidar_target('owl')
    assert calls == ['image', 'pair', 'model', 'cluster']
    assert location['source'] == 'lidar_foreground_cluster'
    assert location['source_frame_id'] == frame['frame_id']
    assert location['source_received_at'] == frame['received_at']
    assert location['odom_session_id'] == 's'
    assert location['lidar_points'] == 4
    assert location['camera_width'] == 640
    assert 'image_bearing_rad' in location
    assert location['camera_measurement_status'] == 'upstream_model_not_per_robot_calibration'


@pytest.mark.asyncio
async def test_lidar_accepts_receipt_skew_before_model(lidar_localizer):
    module, robot, pose, frame, calls = lidar_localizer
    robot.voxel_received_at -= .6
    frame['pose_received_at'] -= .4
    location, _, _ = await module.locate_lidar_target('owl')
    assert location['source'] == 'lidar_foreground_cluster'
    assert calls == ['image', 'pair', 'model', 'cluster']


@pytest.mark.asyncio
async def test_lidar_rejects_missing_sample_before_model(lidar_localizer):
    module, robot, pose, frame, calls = lidar_localizer
    robot.voxel_received_at = None
    with pytest.raises(TargetUnlocalized, match='sensor_sample_unavailable'):
        await module.locate_lidar_target('owl')
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['translation', 'pitch', 'session'])
async def test_lidar_rejects_pose_changed_during_model(lidar_localizer, monkeypatch, change):
    module, robot, pose, frame, calls = lidar_localizer
    changed = dict(pose)
    if change == 'translation':
        changed['x'] = .1
    elif change == 'pitch':
        changed['orientation'] = [0, math.sin(.1), 0, math.cos(.1)]
    else:
        # Real _live_pose performs the session gate independently of position.
        def mismatch(_):
            raise TargetUnlocalized('odom_session_mismatch')
        monkeypatch.setattr(module, '_live_pose', mismatch)
    if change != 'session':
        monkeypatch.setattr(module, '_live_pose', lambda _: changed)
    with pytest.raises(TargetUnlocalized, match='pose_changed|session_mismatch'):
        await module.locate_lidar_target('owl')
    assert calls == ['image', 'pair', 'model']


@pytest.mark.asyncio
async def test_no_lidar_cluster_never_returns_nominal_ray_location(lidar_localizer, monkeypatch):
    from cloud.go2.perception import rgb_lidar
    module, robot, pose, frame, calls = lidar_localizer
    monkeypatch.setattr(rgb_lidar, 'associate_lidar_target', lambda *a, **k: None)
    with pytest.raises(TargetUnlocalized, match='object_returns_unavailable'):
        await module.locate_lidar_target('owl')


def test_real_lidar_target_has_floor_evidence_inside_inflated_keepout():
    from cloud.go2.navigation.occupancy_grid import OccupancyGrid
    floor = [[float(x), float(y), 0.] for x in np.arange(-1., 4.01, .05)
             for y in np.arange(-2., 2.01, .05)]
    toy = [[2. + dx, dy, .25] for dx in (-.05, 0., .05) for dy in (-.05, 0., .05)]
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 's'}
    grid = OccupancyGrid({'data': {'frame_id': 'odom', 'resolution': .05,
        'origin': [-1., -2., -.5], 'width': [105, 85, 30],
        'data': {'points': floor + toy}}}, robot_pose=pose)
    location = {'frame_id': 'odom', 'x': 2., 'y': 0., 'uncertainty_m': .15, 'odom_session_id': 's'}
    assert not grid.is_free(*grid.odom_to_grid(2., 0.))
    stop = select_standoff(grid, pose, location)
    assert grid.is_body_clear(stop['x'], stop['y'], stop['heading'])
    assert stop['distance_to_target_m'] >= 1.4
    grid.measured_floor[:] = False
    with pytest.raises(TargetUnlocalized, match='target_ground_unverified'):
        select_standoff(grid, pose, location)


@pytest.fixture
def visual_search_fallback(monkeypatch):
    """Exercise the real tool composition without devices, models or file stores."""
    from unittest.mock import AsyncMock
    from cloud.go2.tools import tools, executor
    from cloud.go2.navigation import navigator, target_approach
    from cloud.agents.go2.workflows.search import current_search_request
    from cloud.space.execution import ExecutionContext, use_context
    from cloud.observability import events as event_module

    robot = SimpleNamespace(_odom_session_id='fallback-odom')
    primary = AsyncMock(return_value={'result': 'blocked', 'reason': 'object_returns_unavailable',
                                      'target': '小球', 'navigation': None, 'observation_moves': []})
    stop = AsyncMock(return_value={'result': 'ok'})
    owner = SimpleNamespace(owner_session_id='fallback-session')
    calls, events = [], []
    context = ExecutionContext('fallback-session', 'visitor', False,
                               expires_at=int(time.time() + 300))

    async def search(name, params, *, context):
        request = current_search_request()
        calls.append((name, params, context, request))
        assert stop.await_count > 0
        return {'result': 'ok', 'outcome': 'nearby', 'found': True, 'stop': {'result': 'ok'},
                'target': params['target'], 'task_id': request[0], 'target_location': None}

    monkeypatch.setattr(tools, 'go2', robot)
    monkeypatch.setattr(tools.control_lease, 'snapshot', lambda: owner)
    real_navigation = target_approach.navigate_to_visible_target
    real_execute = executor.execute_tool
    monkeypatch.setattr(target_approach, 'navigate_to_visible_target', primary)
    monkeypatch.setattr(navigator.navigator, 'stop_and_confirm', stop)
    monkeypatch.setattr(executor, 'execute_tool', AsyncMock(side_effect=search))
    # Keep the real event contract and redaction; replace only disk output.
    # Mocking emit_agent_event hid an unregistered navigation.fallback event.
    monkeypatch.setattr(event_module, 'write_agent_event',
                        lambda row: events.append((row['event'], row)))
    with use_context(context):
        yield SimpleNamespace(primary=primary, stop=stop, calls=calls, events=events,
                              context=context, robot=robot, owner=owner, tools=tools,
                              real_navigation=real_navigation, real_execute=real_execute,
                              search=executor.execute_tool)


@pytest.mark.parametrize('reason', [
    'localization_unavailable', 'map_unavailable',
    'object_returns_unavailable', 'target_lidar_disagreement', 'position_uncertainty_excessive',
    'sensor_sample_unavailable', 'pose_invalid', 'pose_3d_unavailable',
    'ground_ray_unreliable', 'ground_intersection_unreliable',
    'target_ground_unverified', 'measured_floor_unavailable', 'target_outside_local_map',
])
async def test_visual_target_falls_back_to_existing_search_with_same_goal_and_deadline(visual_search_fallback, reason):
    h = visual_search_fallback
    from cloud.agents.go2.workflows.search import search_request, current_search_request
    from cloud.agents.common.effects import use_effect
    from cloud.go2.tools.registry import target_navigation_complete
    h.primary.return_value['reason'] = reason
    deadline = time.time() + 40
    with search_request('parent-run', deadline), use_effect(None, ('go2', 'thread', 'run', 'step')):
        result = await h.tools.go2_navigate_to_visible_target('小球')
        assert current_search_request() == ('parent-run', deadline)
    name, params, context, request = h.calls[0]
    assert name == 'go2_find_object' and params == {'target': '小球'} and context is h.context
    assert request[1] == deadline
    assert result['initial_navigation'] == h.primary.return_value
    assert result['fallback']['reason'] == reason
    assert result['fallback']['task_id'] == result['task_id'] == request[0]
    assert result['outcome'] == 'nearby' and result['target_location'] is None
    assert 'confirmation' not in result  # Do not fabricate metric arrival.
    assert target_navigation_complete(result, '小球')
    assert h.events[0][0] == 'navigation.fallback'
    h.primary.assert_awaited_once_with('小球', allow_reobservation=False)


@pytest.mark.parametrize('receipt', [
    {'result': 'ok', 'confirmation': 'odom_stopped_and_visual_recheck'},
    {'result': 'blocked', 'reason': 'target_not_visible'},
    {'result': 'blocked', 'reason': 'target_confirmation_mismatch'},
    {'result': 'blocked', 'reason': 'vision_result_invalid'},
    {'result': 'error', 'reason': 'motion_not_observed'},
    {'result': 'cancelled', 'reason': 'localization_unavailable'},
    {'result': 'timeout', 'reason': 'localization_unavailable'},
    {'result': 'blocked', 'reason': 'body_telemetry_unavailable'},
    {'result': 'blocked', 'reason': 'control_lease_changed'},
    {'result': 'blocked', 'reason': 'disconnected'},
    {'result': 'blocked', 'reason': 'no_known_free_path'},
    {'result': 'blocked', 'reason': 'stop_unconfirmed'},
    {'result': 'timeout', 'reason': 'object_returns_unavailable'},
    {'result': 'accepted', 'reason': 'object_returns_unavailable'},
    {'result': 'unknown', 'reason': 'object_returns_unavailable'},
    {'result': 'blocked', 'reason': 'object_returns_unavailable', 'navigation': {'status': 'executing'}},
    {'result': 'blocked', 'reason': 'object_returns_unavailable', 'observation_moves': [{'status': 'unknown'}]},
])
async def test_visual_target_does_not_retry_success_safety_failures_or_unknown_motion(visual_search_fallback, receipt):
    h = visual_search_fallback
    h.primary.return_value = receipt
    assert await h.tools.go2_navigate_to_visible_target('小球') == receipt
    h.search.assert_not_awaited()
    h.stop.assert_not_awaited()


async def test_visual_target_keeps_explicit_distance_limit(visual_search_fallback):
    h = visual_search_fallback
    result = await h.tools.go2_navigate_to_visible_target('小球', max_advance_m=.5)
    h.primary.assert_awaited_once_with('小球', max_advance_m=.5, allow_reobservation=False)
    assert result['arrived'] is True and result['max_advance_m'] == .5
    h.search.assert_awaited_once()
    h.stop.assert_awaited_once()


@pytest.mark.parametrize('change', ['stop_unknown', 'control_changed', 'session_changed'])
async def test_visual_fallback_rechecks_stop_session_and_owner(visual_search_fallback, change):
    h = visual_search_fallback
    async def stop(**_kwargs):
        if change == 'control_changed':
            h.owner.owner_session_id = 'new-user'
        if change == 'session_changed':
            h.robot._odom_session_id = 'new-odom'
        return {'result': 'accepted' if change == 'stop_unknown' else 'ok'}
    h.stop.side_effect = stop
    result = await h.tools.go2_navigate_to_visible_target('小球')
    assert result['reason'] == {'stop_unknown': 'stop_unconfirmed',
        'control_changed': 'control_lease_changed', 'session_changed': 'odom_session_mismatch'}[change]
    h.search.assert_not_awaited()


async def test_visual_fallback_cannot_extend_parent_deadline(visual_search_fallback):
    from cloud.agents.go2.workflows.search import search_request
    h = visual_search_fallback
    with search_request('expired-parent', time.time() - 1):
        result = await h.tools.go2_navigate_to_visible_target('小球')
    assert result == h.primary.return_value
    h.search.assert_not_awaited()
    h.stop.assert_not_awaited()


async def test_visual_fallback_preserves_search_failure_and_completed_motion(visual_search_fallback):
    h = visual_search_fallback
    h.primary.return_value['observation_moves'] = [{'status': 'succeeded', 'task_id': 'observed-move'}]
    h.search.side_effect = None
    h.search.return_value = {'result': 'blocked', 'outcome': 'seen_not_reached',
                            'reason': 'no_known_free_observation_path', 'stop': {'result': 'ok'}}
    result = await h.tools.go2_navigate_to_visible_target('小球')
    assert result['result'] == 'blocked' and result['reason'] == 'no_known_free_observation_path'
    assert result['initial_navigation']['observation_moves'][0]['task_id'] == 'observed-move'
    h.search.assert_awaited_once()


async def test_visual_fallback_identity_is_stable_per_original_effect(visual_search_fallback):
    from cloud.agents.common.effects import use_effect
    h = visual_search_fallback
    for step in ('step-a', 'step-a', 'step-b'):
        with use_effect(None, ('go2', 'thread', 'run', step)):
            await h.tools.go2_navigate_to_visible_target('小球')
    ids = [call[3][0] for call in h.calls]
    assert ids[0] == ids[1] and ids[0] != ids[2]


@pytest.mark.parametrize('reason', ['localization_unavailable', 'map_unavailable'])
async def test_completed_navigation_failure_can_enter_search_after_stopping(visual_search_fallback, reason):
    h = visual_search_fallback
    h.primary.return_value = {
        'result': 'error', 'reason': reason,
        'navigation': {'status': 'failed', 'task_id': 'previous-move',
                       'last_trusted_pose': {'x': .4, 'y': 0., 'session_id': 'fallback-odom'}},
    }
    result = await h.tools.go2_navigate_to_visible_target('小球')
    h.stop.assert_awaited_once()
    h.search.assert_awaited_once()
    assert result['result'] == 'ok'
    assert result['initial_navigation']['navigation']['task_id'] == 'previous-move'


async def test_previous_unconfirmed_stop_requires_new_stop_receipt(visual_search_fallback):
    h = visual_search_fallback
    h.primary.return_value.update(reason='map_unavailable', stop={'result': 'accepted'})
    result = await h.tools.go2_navigate_to_visible_target('小球')
    h.stop.assert_awaited_once()
    h.search.assert_awaited_once()
    assert result['initial_navigation']['stop']['result'] == 'accepted'


async def test_lidar_failure_reaches_search_without_inner_reobservation(visual_search_fallback, monkeypatch):
    """Replay the first failure in the real 14:29 trace, before it was overwritten."""
    from unittest.mock import AsyncMock
    from cloud.go2.navigation import target_approach
    from cloud.go2.perception import visual_target
    h = visual_search_fallback
    # The function is captured by the wrapper at invocation, so retrieve its
    # original implementation from the fixture instead of reloading globals.
    monkeypatch.setattr(target_approach, 'navigate_to_visible_target', h.real_navigation)
    monkeypatch.setattr(target_approach, 'go2', SimpleNamespace(is_connected=True, body_state='standing'))
    monkeypatch.setattr(target_approach, '_authorize', lambda *a, **k: None)
    monkeypatch.setattr(visual_target, 'load_calibration', lambda **k: {})
    locate = AsyncMock(side_effect=TargetUnlocalized('object_returns_unavailable'))
    observe = AsyncMock(side_effect=TargetUnlocalized('localization_unavailable'))
    monkeypatch.setattr(visual_target, 'locate_ground_target', locate)
    monkeypatch.setattr(visual_target, 'observe_target', observe)
    result = await h.tools.go2_navigate_to_visible_target('小球')
    locate.assert_awaited_once()
    observe.assert_not_awaited()
    h.search.assert_awaited_once()
    assert result['fallback']['reason'] == 'object_returns_unavailable'
    assert result['initial_navigation']['reason'] == 'object_returns_unavailable'


@pytest.mark.parametrize('failure', [RuntimeError('vision failure'), asyncio.CancelledError()])
async def test_unrelated_exception_or_cancellation_never_starts_search(visual_search_fallback, failure):
    h = visual_search_fallback
    h.primary.side_effect = failure
    with pytest.raises(type(failure)):
        await h.tools.go2_navigate_to_visible_target('小球')
    h.search.assert_not_awaited()


async def test_fallback_uses_parent_budget_beyond_old_visual_timeout(visual_search_fallback):
    from cloud.agents.go2.workflows.search import search_request, SEARCH_TOOL_TIMEOUT_S
    from cloud.go2.tools.registry import get_tool_spec
    h = visual_search_fallback
    deadline = time.time() + 200
    with search_request('long-search', deadline):
        await h.tools.go2_navigate_to_visible_target('小球')
    assert h.calls[0][3][1] == deadline
    assert get_tool_spec('go2_navigate_to_visible_target').timeout_s == SEARCH_TOOL_TIMEOUT_S


async def test_role_executor_and_search_graph_return_one_final_receipt(
        visual_search_fallback, monkeypatch, tmp_path):
    """Run both production graphs and tool boundaries with only device I/O replaced."""
    from cloud.agents.common.runner import GraphRun, run_role_graph
    from cloud.agents.common.checkpoint import FileCheckpointSaver
    from cloud.agents.common.effects import FileEffectLedger
    from cloud.agents.go2.workflows import search
    from cloud.go2 import connection, control, service
    from cloud.go2.tools import executor
    from unittest.mock import AsyncMock
    h = visual_search_fallback
    h.primary.return_value.update(reason='localization_unavailable')
    h.robot.is_connected = True
    monkeypatch.setenv('SSM_CHECKPOINT_DIR', str(tmp_path / 'search'))
    monkeypatch.setattr(connection, 'go2', h.robot)
    monkeypatch.setattr(executor, 'go2', h.robot)
    monkeypatch.setattr(executor, 'execute_tool', h.real_execute)
    monkeypatch.setattr(executor, '_check_motion_owner', lambda *a: None)
    monkeypatch.setattr(executor, 'check_motion_preconditions', lambda *a: None)
    monkeypatch.setattr(executor, '_audit', lambda name, context, result, *a: result)
    monkeypatch.setattr(service, 'prepare_user_interaction', AsyncMock())
    monkeypatch.setattr(control, 'control_lease', SimpleNamespace(
        acquire=lambda context: None, release=lambda session: None))
    monkeypatch.setattr(control.session_tasks, 'track', lambda *a: None)
    observations = []

    class Device:
        def __init__(self, context, task_id, bounds, session):
            assert context is h.context and session == 'fallback-odom'
            self.travelled_m, self.coverage = 0., {}

        async def guard(self, state):
            return None

        async def progress(self, *a, **k):
            pass

        async def observe(self, state):
            observations.append(state['target'])
            return {'found': True, 'frame_id': len(observations),
                    'observation': {'proximity': 'nearby'}}

        async def record(self, observation):
            return {'observations': len(observations)}

        async def approach(self, state):
            assert state['observation']['nearby_confirmed']
            return {'result': 'ok', 'reason': 'arrival_verified', 'found': True}

    monkeypatch.setattr(search, 'DeviceSearch', Device)

    class Model:
        def bind_tools(self, schemas, **kwargs):
            return self

        async def ainvoke(self, messages):
            raise AssertionError('the same goal must not be dispatched again after fallback')

    run = GraphRun('fallback-chain', h.context.session_id, '去前面的灯那里', time.time() + 40)
    run.agent_id = 'go2'
    async def execute(name, params, step_id):
        with search.search_request(run.run_id, run.deadline):
            return await h.real_execute(name, params, context=h.context)

    await run_role_graph(
        run, model=Model(), tool_schemas={'go2_navigate_to_visible_target': {
            'name': 'go2_navigate_to_visible_target', 'description': 'visual navigation',
            'parameters': {'type': 'object', 'properties': {'target': {'type': 'string'}}}}},
        build_messages=lambda current: [],
        execute=execute, initial_tool=('go2_navigate_to_visible_target', {'target': '小球'}),
        terminal_tools=frozenset({'go2_navigate_to_visible_target'}),
        checkpointer=FileCheckpointSaver(tmp_path / 'role'),
        effect_ledger=FileEffectLedger(tmp_path / 'effects'))
    assert run.status == 'ok' and len(run.steps) == 1
    receipt = run.steps[0]['result']
    assert receipt['outcome'] == 'nearby' and receipt['stop']['result'] == 'ok'
    assert receipt['fallback']['reason'] == 'localization_unavailable'
    assert receipt['initial_navigation']['reason'] == 'localization_unavailable'
    assert observations == ['小球', '小球']
    h.primary.assert_awaited_once()
