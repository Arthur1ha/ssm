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
async def test_visible_target_request_asks_only_for_presence_and_bbox(monkeypatch):
    from cloud.go2 import models
    from cloud.go2.perception import visual_target
    events = []

    class VisionModel:
        def bind(self, **kwargs):
            assert kwargs == {'response_format': {'type': 'json_object'}}
            return self

        async def ainvoke(self, messages):
            prompt = messages[0].content
            assert '"found"' in prompt and '"bbox_1000"' in prompt
            assert '0–1000' in prompt and '不要输出像素坐标' in prompt
            assert all(key not in prompt for key in ('confidence', 'count', 'static'))
            return SimpleNamespace(content='{"found":true,"bbox_1000":[606,567,652,662]}')

    monkeypatch.setattr(models, 'get_vision_llm', lambda: VisionModel())
    monkeypatch.setattr(visual_target, 'emit_agent_event',
                        lambda event, **fields: events.append((event, fields)))
    result = await detect_visible_target(b'jpeg', '白色毛绒玩具',
                                         {'width': 640, 'height': 360, 'frame_id': 43})
    assert result == {'found': True, 'bbox_1000': [606, 567, 652, 662]}
    assert any(event == 'vision.model' and fields.get('phase') == 'parsed'
               and fields['observation'] == result for event, fields in events)


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
async def test_missing_calibration_reports_cause_without_moving(monkeypatch):
    from cloud.go2.tools import tools
    from cloud.go2.perception import visual_target

    monkeypatch.setattr(tools.go2, 'is_connected', True)
    monkeypatch.setattr(tools.go2, 'fsm_state', 'standing')
    monkeypatch.setattr(tools.go2, '_connected_serial', 'robot-serial', raising=False)

    def missing_calibration(*, serial):
        raise TargetUnlocalized('calibration_unavailable')

    monkeypatch.setattr(visual_target, 'load_calibration', missing_calibration)
    result = await tools._go2_precise_navigate_to_visible_target('白色猫头鹰')
    assert result['result'] == 'blocked'
    assert result['reason'] == 'calibration_unavailable'
    assert '相机标定' in result['message']
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
    from cloud.go2.execution import ExecutionContext

    class UnusedModel:
        def bind_tools(self, schemas, parallel_tool_calls=False):
            return self

        async def ainvoke(self, messages):
            raise AssertionError('visual result must not ask a model to change the confirmed outcome')

    async def execute_visual(name, params, *, context):
        assert name == 'go2_navigate_to_visible_target'
        return tool_result

    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    monkeypatch.setattr(graph, 'get_store', lambda: Go2RunView(
        checkpointer=FileCheckpointSaver(tmp_path / 'checkpoints'),
        index=FileRunIndex(tmp_path / 'index')))
    monkeypatch.setattr(graph, 'get_text_llm', lambda: UnusedModel())
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
    if tool_result['result'] == 'ok':
        assert '尚未确认到达' in result['response']
    assert result['actions_taken'] == (['go2_navigate_to_visible_target']
                                       if tool_result['result'] == 'ok' else [])
