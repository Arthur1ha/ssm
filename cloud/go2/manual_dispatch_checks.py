"""Manual state-board commands return a send receipt without posture waits."""
import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud.go2.connection import Go2Connection
from cloud.go2.control import ControlLease
from cloud.space.execution import ExecutionContext


@pytest.fixture
def manual_robot(monkeypatch, tmp_path):
    robot = Go2Connection()
    robot.is_connected = True
    publish = AsyncMock(return_value={'data': {'header': {'status': {'code': 0}}}})
    robot._conn = SimpleNamespace(datachannel=SimpleNamespace(pub_sub=SimpleNamespace(
        publish_request_new=publish)))
    lease = ControlLease()
    for name in ('cloud.go2.router', 'cloud.go2.service', 'cloud.go2.tools.executor'):
        module = importlib.import_module(name)
        monkeypatch.setattr(module, 'go2', robot)
        monkeypatch.setattr(module, 'control_lease', lease)
    service = importlib.import_module('cloud.go2.service')
    monkeypatch.setattr(service, 'prepare_user_interaction', AsyncMock())
    monkeypatch.setattr(service, 'drive', MagicMock())
    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    return robot, publish, lease


@pytest.mark.asyncio
async def test_manual_http_actions_do_not_wait_or_lock_on_missing_posture(manual_robot):
    from cloud.go2.router import go2_command, CommandRequest
    robot, publish, _ = manual_robot
    context = ExecutionContext('manual-user', 'visitor', False)
    # A connected robot can lack all joint telemetry. Neither request needs it.
    for command in ('Sit', 'RiseSit', 'Hello'):
        result = await asyncio.wait_for(go2_command(CommandRequest(cmd=command), context), .5)
        assert result['result'] == 'accepted'
        assert result['dispatch_only'] is True
        assert result['confirmation_scope'] == 'request'
        assert robot.pending_action is None
        assert robot._completion is None
        assert robot._action_deadline is None
        assert robot.runtime.data['life_state'] == 'idle'
        assert robot.runtime.data['last_action']['phase'] == 'dispatched'
        assert robot.body_state is None  # no fabricated physical posture
    assert publish.await_count == 3


@pytest.mark.asyncio
async def test_manual_send_ignores_old_unconfirmed_action_but_keeps_rejection(manual_robot):
    robot, publish, _ = manual_robot
    robot.pending_action = {'command': 'Sit', 'request_id': 'old', 'phase': 'timeout'}
    assert (await robot.send_command('RiseSit', dispatch_only=True))['result'] == 'accepted'
    assert robot.pending_action is None
    publish.return_value = {'data': {'header': {'status': {'code': 7}}}}
    result = await robot.send_command('Sit', dispatch_only=True)
    assert result['result'] == 'error'
    assert result['request_status_code'] == 7
    assert robot.body_state is None
    assert robot.body_confirmation == 'unknown'


@pytest.mark.asyncio
async def test_manual_requests_keep_ownership_parameters_and_idempotency(manual_robot):
    from cloud.go2.tools.executor import execute_command_request
    robot, publish, lease = manual_robot
    context = ExecutionContext('manual-user', 'visitor', False)
    lease.acquire(ExecutionContext('other-user', 'visitor', False))
    blocked = await execute_command_request('Sit', {}, context=context, dispatch_only=True)
    assert blocked['error'] == 'control_lease_conflict'
    publish.assert_not_awaited()
    lease.release('other-user')
    malformed = await execute_command_request('Sit', {'force': True}, context=context, dispatch_only=True)
    assert malformed['result'] in {'blocked', 'error', 'rejected'}
    publish.assert_not_awaited()
    first = await execute_command_request('Sit', {}, context=context, request_id='one', dispatch_only=True)
    again = await execute_command_request('Sit', {}, context=context, request_id='one', dispatch_only=True)
    assert first['result'] == again['result'] == 'accepted'
    assert again['replayed'] is True
    publish.assert_awaited_once()
    assert not getattr(robot, '_command_result_records', {})


def test_manual_availability_does_not_enable_autonomous_motion(manual_robot):
    robot, _, _ = manual_robot
    state = robot.state_snapshot()
    assert 'Sit' in state['available_manual_actions']
    assert state['available_actions'] == ['StopMove']
    with pytest.raises(ValueError):
        robot._validate_motion('Move', {'x': .1})
    robot.runtime.commit('rest', activity_state='rest')
    assert robot.state_snapshot()['available_manual_actions'] == []
    with pytest.raises(ValueError):
        robot._validate_motion('Sit', dispatch_only=True)
    robot.is_connected = False
    assert robot.state_snapshot()['available_manual_actions'] == []
