"""Cross-transport life facts through the real tool boundary, without hardware."""

import asyncio
import importlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud.go2.connection import Go2Connection
from cloud.go2.control import ControlLease
from cloud.go2.execution import ExecutionContext


@pytest.fixture
def runtime(monkeypatch):
    router = importlib.import_module('cloud.go2.router')
    service = importlib.import_module('cloud.go2.service')
    tools = importlib.import_module('cloud.go2.tools.tools')
    executor = importlib.import_module('cloud.go2.tools.executor')
    card = importlib.import_module('cloud.go2.card')
    navigation = importlib.import_module('cloud.go2.navigation.navigator')
    robot = Go2Connection()
    robot.is_connected = True
    # Trusted test fixture observation, never a production posture override API.
    robot.runtime.body('lying', confirmation='observed')
    robot._last_state_received_at = time.monotonic()
    robot._sport_state_observed_at = time.monotonic()
    physical = AsyncMock(side_effect=AssertionError('life control must not send body commands'))
    monkeypatch.setattr(robot, 'send_command', physical)
    lease = ControlLease()
    for module in (router, tools, executor, card, service):
        monkeypatch.setattr(module, 'go2', robot)
    for module in (router, tools, executor, service):
        monkeypatch.setattr(module, 'control_lease', lease)
    monkeypatch.setattr(service, 'drive', SimpleNamespace(stop=MagicMock(), owner_session_id=None))
    monkeypatch.setattr(navigation, 'navigator', SimpleNamespace(mode=SimpleNamespace(value='idle')))
    monkeypatch.setattr(executor, '_audit', lambda _name, _context, result, *args: result)
    mqtt = MagicMock()
    monkeypatch.setattr(card, '_mqtt_client', mqtt)
    context = ExecutionContext('life-integration', 'visitor', False,
                               expires_at=int(time.time()) + 60)
    return SimpleNamespace(robot=robot, router=router, tools=tools, executor=executor,
                           card=card, mqtt=mqtt, context=context, physical=physical, lease=lease)


@pytest.mark.asyncio
async def test_life_change_pushes_sse_and_matches_http_mqtt_without_sensor_frame(runtime):
    r = runtime
    response = await r.router.go2_state(context=r.context)
    stream = response.body_iterator
    event = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    try:
        result = await r.executor.execute_tool('go2_life', {'action': 'request_rest'}, context=r.context)
        assert result['result'] == 'ok'
        sse_text = await asyncio.wait_for(event, timeout=.5)
        if isinstance(sse_text, bytes):
            sse_text = sse_text.decode()
        sse = json.loads(sse_text.removeprefix('data: ').strip())
        http = await r.router.go2_status(context=r.context)
        r.card._publish_go2_state()
        mqtt = json.loads(r.mqtt.publish.call_args.args[1])
        for field in ('state_schema_version', 'epoch', 'revision', 'connection_state',
                      'activity_state', 'life_state', 'body_state', 'fsm_state',
                      'body_confirmation', 'pending_action', 'control_mode'):
            assert result[field] == http[field] == sse[field] == mqtt[field]
        assert http['activity_state'] == 'rest'
        assert http['body_state'] == 'lying'
        assert http['life_state'] is None
        assert http['connected'] is True
        assert r.robot._robot_state == {}  # No hardware frame was needed for the life event.
        assert 'wake' in http['available_life_actions']
        r.physical.assert_not_awaited()
    finally:
        if not event.done():
            event.cancel()
            await asyncio.gather(event, return_exceptions=True)
        await stream.aclose()
    assert r.robot._state_queues == []


@pytest.mark.asyncio
async def test_wake_preserves_pose_and_configuration_without_replaying_exploration(runtime):
    r = runtime
    r.robot.runtime.commit('autonomy_config', autonomy_mode='free_explore')
    r.robot.runtime.exploration_enabled = True
    assert (await r.executor.execute_tool('go2_life', {'action': 'request_rest'},
                                          context=r.context))['result'] == 'ok'
    blocked = await r.executor.execute_tool('go2_sport', {'cmd': 'StandUp'}, context=r.context)
    assert blocked['result'] == 'blocked'
    result = await r.executor.execute_tool('go2_life', {'action': 'wake'}, context=r.context)
    assert result['result'] == 'ok'
    assert result['activity_state'] == 'active'
    assert result['life_state'] == 'idle'
    assert result['body_state'] == 'lying'
    assert result['autonomy_mode'] == 'free_explore'
    assert not r.robot.runtime.exploration_enabled
    r.physical.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_rest_is_rejected_without_delayed_transition(runtime):
    r = runtime
    token = r.robot.runtime.begin_life('play', r.context.session_id, 'interaction')
    result = await r.executor.execute_tool('go2_life', {'action': 'request_rest'}, context=r.context)
    assert result['result'] == 'blocked'
    r.robot.runtime.finish_life(token)
    await asyncio.sleep(0)
    assert r.robot.state_snapshot()['activity_state'] == 'active'
    assert r.robot.state_snapshot()['life_state'] == 'idle'
    r.physical.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('life', ['rest', 'play'])
async def test_stop_explore_does_not_rewrite_unrelated_life_state(runtime, life):
    r = runtime
    if life == 'rest':
        r.robot.runtime.request_rest()
    else:
        r.robot.runtime.begin_life('play', 'other-session', 'other-interaction')
    before = r.robot.state_snapshot()
    result = await r.executor.execute_tool('go2_life', {'action': 'stop_explore'}, context=r.context)
    assert result['result'] == 'blocked'
    after = r.robot.state_snapshot()
    for field in ('activity_state', 'life_state', 'life_owner', 'life_task_id'):
        assert after[field] == before[field]
    r.physical.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_admin_rest_does_not_preempt_current_motion_lease(runtime):
    r = runtime
    current = ExecutionContext('current-owner', 'visitor', False)
    r.lease.acquire(current)
    r.robot.runtime.begin_life('play', current.session_id, 'active-interaction')
    admin = ExecutionContext('admin-request', 'admin', True)
    result = await r.executor.execute_tool('go2_life', {'action': 'request_rest'}, context=admin)
    assert result['result'] == 'blocked'
    assert r.lease.snapshot().owner_session_id == current.session_id
    assert r.robot.state_snapshot()['life_task_id'] == 'active-interaction'


def test_freshness_gate_changes_advance_version_and_reach_mqtt_registry(runtime):
    from cloud.cards.registry import CardRegistry
    r = runtime
    registry = CardRegistry()

    def publish():
        r.card._publish_go2_state()
        topic, payload = r.mqtt.publish.call_args.args
        registry.handle_message(topic, payload)
        return json.loads(payload)

    fresh = publish()
    assert 'StandUp' in fresh['available_actions']
    r.robot._sport_state_observed_at = time.monotonic() - 4
    stale = publish()
    assert stale['revision'] > fresh['revision']
    assert 'StandUp' not in stale['available_actions']
    assert registry._states['go2']['available_actions'] == stale['available_actions']
    r.robot._on_state({'data': {'data': {'velocity': [0, 0, 0], 'body_height': .1}}})
    recovered = publish()
    assert recovered['revision'] > stale['revision']
    assert 'StandUp' in recovered['available_actions']
    assert registry._states['go2']['available_actions'] == recovered['available_actions']


def test_exploration_permission_changes_are_versioned_for_transport_consumers(runtime):
    from cloud.cards.registry import CardRegistry
    r = runtime
    registry = CardRegistry()
    previous = r.robot.state_snapshot()
    for enabled in (True, False):
        r.robot.runtime.exploration_enabled = enabled
        r.card._publish_go2_state()
        topic, serialized = r.mqtt.publish.call_args.args
        payload = json.loads(serialized)
        assert payload['revision'] > previous['revision']
        assert ('stop_explore' in payload['available_life_actions']) is enabled
        registry.handle_message(topic, serialized)
        assert registry._states['go2']['available_life_actions'] == payload['available_life_actions']
        previous = payload
