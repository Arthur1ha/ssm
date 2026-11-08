"""State-board action preparation, with simulated physical receipts only."""

import asyncio
import importlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud.agents.go2.workflows.body_action import start_body_action
from cloud.go2.actions import ACTIONS, SPORT_COMMANDS
from cloud.go2.connection import Go2Connection
from cloud.go2.control import ControlLease
from cloud.go2.execution import ExecutionContext

USER = ExecutionContext('body-action-user', 'visitor', False)
TARGETS = [cmd for cmd in SPORT_COMMANDS if ACTIONS[cmd].source_states == ('standing',)] + ['EnterMoveControl']


@pytest.fixture
def robot(monkeypatch, tmp_path):
    robot = Go2Connection()
    robot.is_connected = True
    robot.runtime.body('sitting', confirmation='observed')
    robot._sport_state_observed_at = time.monotonic()
    robot._joint_observation_available = True
    robot._last_motor_observed_at = time.monotonic()
    robot.sent = []

    async def publish(topic, options):
        robot.sent.append(options['api_id'])
        return {'data': {'header': {'status': {'code': 0}}}}

    robot._conn = SimpleNamespace(datachannel=SimpleNamespace(pub_sub=SimpleNamespace(
        publish_request_new=publish, publish_without_callback=lambda *a, **kw: None)))
    lease = ControlLease()
    for name in ('cloud.go2.router', 'cloud.go2.service', 'cloud.go2.tools.executor'):
        module = importlib.import_module(name)
        monkeypatch.setattr(module, 'go2', robot)
        monkeypatch.setattr(module, 'control_lease', lease)
    service = importlib.import_module('cloud.go2.service')
    monkeypatch.setattr(service, 'prepare_user_interaction', AsyncMock())
    monkeypatch.setattr(service, 'drive', MagicMock())
    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path / 'training'))
    robot._sport_state_observed_at = time.monotonic()
    robot._last_motor_observed_at = time.monotonic()
    return robot


async def start(robot, command, **kwargs):
    from cloud.go2.tasks import chat_tasks
    result = await start_body_action(command, {}, context=USER, **kwargs)
    for _ in range(100):
        if robot.sent or chat_tasks.get(result['task_id'], USER.session_id).result is not None:
            break
        await asyncio.sleep(.05)
    return chat_tasks.get(result['task_id'], USER.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('source,stand', [('sitting', 'RiseSit'), ('lying', 'StandUp')])
@pytest.mark.parametrize('command', TARGETS)
async def test_every_standing_action_waits_for_correct_rise_receipt(robot, source, stand, command):
    from cloud.go2.tasks import chat_tasks
    robot.runtime.body(source, confirmation='observed')
    record = await start(robot, command)
    assert robot.sent == [ACTIONS[stand].api_id]
    assert record.result is None
    robot._complete_observed_action('standing')
    result = await chat_tasks.wait(record, 5)
    assert result['result'] == 'accepted'
    assert result['preparation']['result'] == 'ok'
    if command == 'EnterMoveControl':
        assert robot.sent == [ACTIONS[stand].api_id, ACTIONS['BalanceStand'].api_id, 1027]
        assert robot.runtime.data['control_mode'] == 'remote'
    else:
        assert robot.sent == [ACTIONS[stand].api_id, ACTIONS[command].api_id]
        assert result['dispatch_only'] is True
        assert robot.fsm_state is None  # target acceptance is not its completion


@pytest.mark.asyncio
async def test_rpc_only_rise_never_sends_target(robot):
    from cloud.go2.tasks import chat_tasks
    robot._joint_observation_available = False
    record = await start(robot, 'StandDown')
    result = await chat_tasks.wait(record, 5)
    assert result['result'] == 'blocked'
    assert '起身尚未确认' in result['error']
    assert robot.sent == [ACTIONS['RiseSit'].api_id]
    robot._finish_pending('cancelled', 'test_cleanup')


@pytest.mark.asyncio
async def test_failed_rise_never_sends_target(robot):
    from cloud.go2.tasks import chat_tasks
    record = await start(robot, 'Hello')
    robot._expire_body_action(robot.pending_action['request_id'])
    robot._completion.set_result({'result':'timeout','error':'completion_not_observed'})
    result = await chat_tasks.wait(record, 1)
    assert result['result'] == 'timeout'
    assert robot.sent == [ACTIONS['RiseSit'].api_id]


@pytest.mark.asyncio
@pytest.mark.parametrize('condition', ['stale', 'unknown', 'rest', 'other_owner'])
async def test_unavailable_source_never_sends_preparation(robot, condition):
    if condition == 'stale':
        robot._sport_state_observed_at = time.monotonic() - 10
    elif condition == 'unknown':
        robot.runtime.body(None, confirmation='unknown')
    elif condition == 'rest':
        robot.runtime.commit('test_rest', activity_state='rest')
    else:
        executor = importlib.import_module('cloud.go2.tools.executor')
        executor.control_lease.acquire(ExecutionContext('someone-else', 'visitor', False))
    record = await start(robot, 'Hello')
    assert record.result['result'] == 'blocked'
    assert robot.sent == []


@pytest.mark.asyncio
async def test_retry_shares_task_and_never_repeats_physical_commands(robot):
    from cloud.go2.tasks import chat_tasks
    request_id = 'retry_' + str(time.time_ns())
    first = await start(robot, 'StandDown', request_id=request_id)
    again = await start(robot, 'StandDown', request_id=request_id)
    assert first is again
    assert len(robot.sent) == 1
    robot._complete_observed_action('standing')
    await chat_tasks.wait(first, 1)
    assert await start(robot, 'StandDown', request_id=request_id) is first
    assert len(robot.sent) == 2


@pytest.mark.asyncio
async def test_emergency_stop_cancels_preparation_and_prevents_target(robot, monkeypatch):
    from cloud.go2.router import emergency_stop
    from cloud.go2.tasks import chat_tasks
    router = importlib.import_module('cloud.go2.router')
    monkeypatch.setattr(router, '_configure_vision_consumers', MagicMock())
    record = await start(robot, 'Hello')
    await emergency_stop(USER)
    await chat_tasks.wait(record, 1)
    assert record.task.cancelled()
    assert robot.sent == [ACTIONS['RiseSit'].api_id]


@pytest.mark.asyncio
async def test_gateway_forwards_preparation_as_request_metadata(monkeypatch):
    from cloud.api import intents
    dispatch = AsyncMock(return_value={'result':'executing','task_id':'body_test'})
    monkeypatch.setattr(intents, '_internal_request', dispatch)
    session = {'actor':'visitor','session_id':'body-test-session'}
    await intents._dispatch_go2('SPORT', {'cmd':'StandDown','prepare_standing':True}, session)
    dispatch.assert_awaited_once_with('/api/go2/commands','POST',session,
                                     {'cmd':'StandDown','params':{},'prepare_standing':True})


@pytest.mark.asyncio
async def test_http_command_uses_preparation_workflow(robot):
    from cloud.go2.router import go2_command, CommandRequest
    from cloud.go2.tasks import chat_tasks
    result = await go2_command(CommandRequest(cmd='Hello', prepare_standing=True), USER)
    record = chat_tasks.get(result['task_id'], USER.session_id)
    assert robot.sent == [ACTIONS['RiseSit'].api_id]
    robot._complete_observed_action('standing')
    assert (await chat_tasks.wait(record, 5))['result'] == 'accepted'
    assert robot.sent[-1] == ACTIONS['Hello'].api_id


@pytest.mark.asyncio
async def test_moving_to_body_action_waits_for_observed_stop(robot):
    from cloud.go2.tasks import chat_tasks
    robot.runtime.body('moving', confirmation='observed')
    robot._move_control_active = True
    robot._velocity_command_active = True
    robot._motion_observation_enabled = True
    record = await start(robot, 'Sit')
    assert robot.sent == [ACTIONS['StopMove'].api_id]
    assert record.result is None
    assert robot.runtime.data['control_mode'] == 'normal'
    robot.move_velocity(0, 0, 0)  # disabling the UI also releases its stick
    for _ in range(3):
        robot._on_state({'data':{'velocity':[0,0,0]}})
    result = await chat_tasks.wait(record, 5)
    assert result['result'] == 'accepted'
    assert robot.sent == [ACTIONS['StopMove'].api_id,ACTIONS['Sit'].api_id]
    assert robot._state_queues == []
