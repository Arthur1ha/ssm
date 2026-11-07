"""Direct actions share the tool boundary and durably suppress ambiguous retries."""
import asyncio
import importlib
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from cloud.agents.common.effects import FileEffectLedger
from cloud.go2.connection import Go2Connection
from cloud.go2.control import ControlLease
from cloud.go2.execution import ExecutionContext
from cloud.go2.tasks import command_identity, read_command_result

USER = ExecutionContext('commands-user', 'visitor', False)


@pytest.fixture
def device(monkeypatch, tmp_path):
    robot = Go2Connection()
    robot.is_connected = True
    robot.runtime.body('standing', confirmation='observed')
    robot._sport_state_observed_at = time.monotonic()
    robot.send_command = AsyncMock(return_value={
        'result': 'accepted', 'request_id': 'physical-1',
        'confirmation_scope': 'request', 'request_status_code': 0})
    lease = ControlLease()
    for name in ('cloud.go2.router', 'cloud.go2.service', 'cloud.go2.tools.executor', 'cloud.go2.tools.tools'):
        module = importlib.import_module(name)
        monkeypatch.setattr(module, 'go2', robot)
        monkeypatch.setattr(module, 'control_lease', lease)
    service = importlib.import_module('cloud.go2.service')
    monkeypatch.setattr(service, 'drive', MagicMock(_task=None))
    executor = importlib.import_module('cloud.go2.tools.executor')
    monkeypatch.setattr(executor, '_audit', lambda _name, _context, result, *_args: result)
    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    robot._sport_state_observed_at = time.monotonic()
    return robot, lease


@pytest.mark.asyncio
async def test_same_request_returns_receipt_without_rechecking_changed_pose(device):
    from cloud.go2.tools.executor import execute_command_request
    robot, _ = device
    first = await execute_command_request('Hello', {}, context=USER, request_id='wave-1')
    robot.runtime.body(None, confirmation='unknown')
    retry = await execute_command_request('Hello', {}, context=USER, request_id='wave-1')
    assert first['result'] == retry['result'] == 'accepted'
    assert retry['replayed'] is True
    assert retry['request_id'] == 'physical-1'
    assert retry['command_request_id'] == 'wave-1'
    assert first['trace_id'] == retry['trace_id']
    robot.send_command.assert_awaited_once_with('Hello')


@pytest.mark.asyncio
async def test_changed_params_conflict_but_new_request_dispatches_again(device):
    from cloud.go2.tools.executor import execute_command_request
    robot, _ = device
    await execute_command_request('Hello', {}, context=USER, request_id='wave-1')
    with pytest.raises(ValueError, match='effect_call_id_conflict'):
        await execute_command_request('Hello', {'force': True}, context=USER, request_id='wave-1')
    second = await execute_command_request('Hello', {}, context=USER, request_id='wave-2')
    assert second['result'] == 'accepted'
    assert robot.send_command.await_count == 2


@pytest.mark.asyncio
async def test_orphan_reservation_never_redispatches_after_recovery(device):
    from cloud.go2.tools.executor import execute_command_request
    robot, _ = device
    FileEffectLedger().reserve(*command_identity(USER, 'crashed'), 'Hello', {})
    result = await execute_command_request('Hello', {}, context=USER, request_id='crashed')
    assert result['result'] == 'accepted'
    assert result['error'] == 'command_result_unconfirmed'
    assert read_command_result(USER, 'crashed')['error'] == 'command_result_unconfirmed'
    robot.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_inflight_retry_is_single_dispatch_and_final_receipt_is_queryable(device):
    from cloud.go2.tools.executor import execute_command_request
    robot, _ = device
    entered, finish = asyncio.Event(), asyncio.Event()
    async def slow(command):
        entered.set()
        await finish.wait()
        return {'result': 'ok', 'request_id': 'physical-1', 'confirmation_scope': 'completion'}
    robot.send_command.side_effect = slow
    task = asyncio.create_task(execute_command_request('Hello', {}, context=USER, request_id='slow'))
    await entered.wait()
    retry = await execute_command_request('Hello', {}, context=USER, request_id='slow')
    assert retry['error'] == 'command_result_unconfirmed'
    assert robot.send_command.await_count == 1
    finish.set()
    await task
    assert read_command_result(USER, 'slow')['result'] == 'ok'


@pytest.mark.asyncio
async def test_query_is_scoped_to_trusted_origin_and_session(device):
    from cloud.go2.tools.executor import execute_command_request
    from cloud.go2.router import go2_command_result
    await execute_command_request('Hello', {}, context=USER, request_id='private')
    for outsider in (ExecutionContext('other', 'visitor', False),
                     ExecutionContext(USER.session_id, 'visitor', False, origin_kind='agent_autonomous')):
        assert read_command_result(outsider, 'private') is None
        with pytest.raises(HTTPException) as exc:
            await go2_command_result('private', context=outsider)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_manual_button_uses_send_receipt_while_tool_retains_observation_guard(device):
    from cloud.go2.router import CommandRequest, go2_command
    from cloud.go2.tools.executor import execute_tool
    robot, _ = device
    page = await go2_command(CommandRequest(cmd='Hello', request_id='page'), context=USER)
    tool = await execute_tool('go2_sport', {'cmd': 'Hello'}, context=USER)
    for key in ('result', 'action', 'confirmation_scope', 'request_status_code'):
        assert page[key] == tool[key]
    assert robot.send_command.await_count == 2
    robot.runtime.body(None, confirmation='unknown')
    page = await go2_command(CommandRequest(cmd='Hello'), context=USER)
    assert page['result'] == 'accepted'
    robot.send_command.assert_awaited_with('Hello', {}, dispatch_only=True)
    tool = await execute_tool('go2_sport', {'cmd': 'Hello'}, context=USER)
    assert tool['error'] == 'device_precondition'
    assert robot.send_command.await_count == 3


@pytest.mark.asyncio
async def test_ordinary_stop_has_same_session_ownership_for_page_and_tool(device):
    from cloud.go2.tools.executor import execute_tool, execute_command_request
    robot, lease = device
    lease.acquire(ExecutionContext('someone-else', 'visitor', False))
    page = await execute_command_request('StopMove', {}, context=USER)
    tool = await execute_tool('go2_sport', {'cmd': 'StopMove'}, context=USER)
    assert page['error'] == tool['error'] == 'control_lease_conflict'
    robot.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_or_invalid_context_never_dispatches(device):
    from cloud.go2.tools.executor import execute_action
    robot, _ = device
    for context in (ExecutionContext('expired', 'visitor', False, expires_at=1),
                    ExecutionContext('untrusted', 'agent', False)):
        with pytest.raises(ValueError):
            await execute_action('Hello', context=context)
    robot.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_feedback_requires_same_physical_request_and_connection_epoch(device):
    from cloud.go2.tools.executor import execute_command_request
    robot, _ = device
    await execute_command_request('Hello', {}, context=USER, request_id='late')
    last = {'request_id': 'physical-1', 'result': 'ok', 'confirmation': 'joint_posture'}
    robot.runtime.commit('new_connection', epoch='new-epoch')
    robot.runtime.commit('old_physical_complete', last_action=last)
    assert read_command_result(USER, 'late', robot=robot)['result'] == 'accepted'


def test_mode_read_has_no_router_copy(device):
    from cloud.go2 import service
    robot, _ = device
    robot.runtime.commit('mode_test', autonomy_mode='free_explore')
    assert service.get_autonomy_mode() == 'free_explore'
    robot.runtime.commit('mode_test', autonomy_mode='manual')
    assert service.get_autonomy_mode() == 'manual'


@pytest.mark.asyncio
async def test_live_interaction_owner_blocks_other_session_even_without_lease(device):
    from cloud.go2.tools.executor import execute_action
    robot, lease = device
    robot.runtime.begin_life('play', 'other-session', 'model-still-thinking')
    assert lease.snapshot() is None
    with pytest.raises(ValueError, match='interaction_owned_by_another_session'):
        await execute_action('Hello', context=USER)
    robot.send_command.assert_not_awaited()
    assert robot.runtime.data['life_task_id'] == 'model-still-thinking'


@pytest.mark.asyncio
async def test_retry_after_deadline_reads_existing_record_without_dispatch(device):
    from cloud.go2.tools.executor import execute_command_request
    robot, _ = device
    await execute_command_request('Hello', {}, context=USER, request_id='past-deadline')
    result = await execute_command_request('Hello', {}, context=USER,
                                         request_id='past-deadline', deadline=time.time() - 10)
    assert result['result'] == 'accepted'
    assert result['replayed'] is True
    robot.send_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_or_duplicate_requests_do_not_repeat_global_intervention(device, monkeypatch):
    from cloud.go2.tools.executor import execute_command_request
    from cloud.api import activity
    robot, _ = device
    intervention = AsyncMock()
    monkeypatch.setattr(activity, 'user_intervention', intervention)
    user = ExecutionContext('trusted-user', 'visitor', False, expires_at=int(time.time()) + 60)
    rejected = await execute_command_request('Hello', {'force': True}, context=user, request_id='invalid')
    assert rejected['result'] == 'blocked'
    intervention.assert_not_awaited()
    await execute_command_request('Hello', {}, context=user, request_id='valid')
    await execute_command_request('Hello', {}, context=user, request_id='valid')
    intervention.assert_awaited_once()
    robot.send_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_activity_shutdown_requires_fresh_second_preflight(device, monkeypatch):
    from cloud.go2.tools.executor import execute_command_request
    from cloud.api import activity
    robot, _ = device
    async def stop_changed_posture():
        robot.runtime.body(None, confirmation='unknown')
    monkeypatch.setattr(activity, 'user_intervention', stop_changed_posture)
    user = ExecutionContext('trusted-user', 'visitor', False, expires_at=int(time.time()) + 60)
    result = await execute_command_request('Hello', {}, context=user)
    assert result['result'] == 'blocked'
    robot.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_timed_move_keeps_interaction_owned_through_stop_request(device):
    from cloud.go2.tools.executor import execute_tool
    robot, _ = device
    observed = []
    async def send(command, params=None):
        observed.append((command, robot.runtime.data['life_state'], robot.runtime.data['life_owner']))
        return {'result': 'accepted', 'confirmation_scope': 'request'}
    robot.send_command.side_effect = send
    result = await execute_tool('go2_move', {'direction': 'forward', 'duration': .01}, context=USER)
    assert result['result'] == 'accepted'
    assert observed == [('Move', 'play', USER.session_id), ('StopMove', 'play', USER.session_id)]
    assert robot.runtime.data['life_state'] == 'idle'


@pytest.mark.asyncio
async def test_completion_survives_next_action_and_process_recovery(device):
    from cloud.go2.tools.executor import execute_command_request
    robot, _ = device
    await execute_command_request('Hello', {}, context=USER, request_id='completed-a')
    robot.runtime.commit('action_a_complete', last_action={
        'request_id': 'physical-1', 'result': 'ok', 'confirmation': 'joint_posture'})
    # No query between A's completion and B's completion: the observation
    # callback must preserve A before last_action is replaced.
    robot.runtime.commit('action_b_complete', last_action={
        'request_id': 'physical-2', 'result': 'ok', 'confirmation': 'joint_posture'})
    assert read_command_result(USER, 'completed-a', robot=robot)['result'] == 'ok'
    robot._command_result_records.clear()
    robot.runtime.commit('reconnected', epoch='next-connection', last_action=None)
    recovered = read_command_result(USER, 'completed-a')
    assert recovered['result'] == 'ok'
    assert recovered['last_action']['request_id'] == 'physical-1'
    assert recovered['confirmation_scope'] == 'completion'


@pytest.mark.asyncio
async def test_timeout_with_correlated_physical_id_accepts_later_completion(device):
    from cloud.go2.tools.executor import execute_command_request
    robot, _ = device
    entered = asyncio.Event()
    async def pending(command):
        robot.runtime.commit('dispatch', pending_action={
            'request_id': 'timeout-physical', 'command': command,
            'life_task_id': robot.runtime.data['life_task_id']})
        entered.set()
        await asyncio.Event().wait()
    robot.send_command.side_effect = pending
    result = await execute_command_request('Hello', {}, context=USER,
                                          request_id='timed-out', deadline=time.time() + .05)
    assert entered.is_set()
    assert result['result'] == 'timeout'
    assert result['request_id'] == 'timeout-physical'
    robot.runtime.finish_body_action('timeout-physical', 'ok', phase='completed', confirmation='joint_posture')
    completed = read_command_result(USER, 'timed-out', robot=robot)
    assert completed['result'] == 'ok'
    assert 'error' not in completed
    assert completed['confirmation_scope'] == 'completion'
    robot.send_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_physical_timeout_and_late_completion_are_both_durable(device):
    from cloud.go2.tools.executor import execute_command_request
    robot, _ = device
    await execute_command_request('Hello', {}, context=USER, request_id='late-after-physical-timeout')
    robot.runtime.commit('observed_timeout', last_action={
        'request_id': 'physical-1', 'result': 'timeout', 'phase': 'timeout',
        'error': 'completion_not_observed'})
    assert read_command_result(USER, 'late-after-physical-timeout')['result'] == 'timeout'
    robot.runtime.commit('late_completion', last_action={
        'request_id': 'physical-1', 'result': 'ok', 'phase': 'completed',
        'confirmation': 'joint_posture'})
    assert read_command_result(USER, 'late-after-physical-timeout')['result'] == 'ok'


@pytest.mark.asyncio
async def test_direct_trace_reaches_dispatch_and_late_result_events(device, monkeypatch):
    from cloud.go2.tools.executor import execute_command_request
    from cloud.observability import current_event_context
    from cloud.observability import events
    robot, _ = device
    emitted, contexts = [], []
    monkeypatch.setattr(events, 'write_agent_event', emitted.append)
    async def send(command):
        contexts.append(current_event_context())
        return {'result': 'accepted', 'request_id': 'traced-physical', 'confirmation_scope': 'request'}
    robot.send_command.side_effect = send
    trace_id = 'tr_' + '1' * 32
    await execute_command_request('Hello', {}, context=USER, request_id='traced-request', trace_id=trace_id)
    await execute_command_request('Hello', {}, context=USER, request_id='traced-request', trace_id=trace_id)
    assert contexts == [{
        'trace_id': trace_id, 'agent_id': 'go2', 'session_id': USER.session_id,
        'task_id': 'traced-request', 'request_id': 'traced-request', 'origin_kind': 'user_delegated'}]
    commands = [event for event in emitted if event['event'] == 'device.command']
    assert len(commands) == 1
    assert commands[0]['trace_id'] == trace_id
    robot.runtime.commit('late_complete', last_action={
        'request_id': 'traced-physical', 'result': 'ok', 'confirmation': 'joint_posture'})
    physical = [event for event in emitted if event['event'] == 'device.result']
    assert [event['status'] for event in physical] == ['accepted', 'ok']
    assert all(event['trace_id'] == trace_id and event['task_id'] == 'traced-request' for event in physical)
    assert physical[-1]['request_id'] == 'traced-physical'


def test_read_routes_have_no_intervening_dependency():
    from cloud.go2 import router
    def dependencies(dependant):
        for dependency in dependant.dependencies:
            yield dependency.call
            yield from dependencies(dependency)
    for route in router.router.routes:
        if 'GET' in route.methods:
            assert router.require_intervening_service_context not in dependencies(route.dependant), route.path


@pytest.mark.asyncio
async def test_http_chat_retry_status_resume_and_stream_do_not_intervene(device, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from cloud.go2 import router, tasks
    from cloud.agents.go2 import graph
    from cloud.api import activity
    robot, _ = device
    user = ExecutionContext('http-chat-user', 'visitor', False, expires_at=int(time.time()) + 60)
    intervention = AsyncMock()
    pending_cancel = MagicMock()
    run = AsyncMock(return_value={'result': 'ok', 'response': 'read only response'})
    monkeypatch.setattr(activity, 'user_intervention', intervention)
    monkeypatch.setattr(router, '_cancel_pending_peer_requests', pending_cancel)
    monkeypatch.setattr(graph, 'run_go2_loop', run)
    monkeypatch.setattr(tasks, 'chat_tasks', tasks.ChatTaskRegistry())
    monkeypatch.setattr(router.drive, 'user_interrupt', True)
    app = FastAPI()
    app.include_router(router.router)
    async def verified_context():
        return user
    app.dependency_overrides[router.require_service_context] = verified_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        body = {'message': 'status please', 'task_id': 'chat-retry', 'sync_window_s': 1}
        first = await client.post('/api/go2/chat', json=body)
        retry = await client.post('/api/go2/chat', json=body)
        status = await client.get('/api/go2/tasks/chat-retry')
        assert first.status_code == retry.status_code == status.status_code == 200
        assert first.json() == retry.json() == status.json()
        run.assert_awaited_once()
        resumed = await client.post('/api/go2/tasks/chat-retry/resume', json={'message': 'continue'})
        assert resumed.status_code == 200
        streamed = await client.post('/api/go2/chat/stream', json={'message': 'read status', 'task_id': 'chat-retry'})
        assert streamed.status_code == 200
        assert '"type": "done"' in streamed.text
    intervention.assert_not_awaited()
    pending_cancel.assert_not_called()
    assert router.drive.user_interrupt is True
    robot.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_heartbeat_is_non_intervening_but_new_motion_uses_boundary(device, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from cloud.go2 import router
    from cloud.api import activity
    robot, lease = device
    user = ExecutionContext('http-motion-user', 'visitor', False, expires_at=int(time.time()) + 60)
    intervention = AsyncMock()
    monkeypatch.setattr(activity, 'user_intervention', intervention)
    app = FastAPI()
    app.include_router(router.router)
    async def verified_context():
        return user
    app.dependency_overrides[router.require_service_context] = verified_context
    lease.acquire(user)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        heartbeat = await client.post('/api/go2/control/lease/heartbeat')
        assert heartbeat.status_code == 200
        intervention.assert_not_awaited()
        command = await client.post('/api/go2/commands', json={'cmd': 'Hello', 'request_id': 'http-command'})
        assert command.status_code == 200
        intervention.assert_awaited_once()
        repeat = await client.post('/api/go2/commands', json={'cmd': 'Hello', 'request_id': 'http-command'})
        assert repeat.status_code == 200
        intervention.assert_awaited_once()
    robot.send_command.assert_awaited_once_with('Hello', {}, dispatch_only=True)


@pytest.mark.asyncio
async def test_http_emergency_stop_precedes_blocked_activity_cleanup_without_pose(device, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from cloud.go2 import router, service
    from cloud.go2.navigation import navigator as navigation
    from cloud.api import activity
    from cloud.peers import gateway
    robot, _ = device
    user = ExecutionContext('emergency-user', 'visitor', False, expires_at=int(time.time()) + 60)
    robot.runtime.body(None, confirmation='unknown')
    robot.pending_action = {'command': 'FrontJump', 'phase': 'accepted'}
    monkeypatch.setattr(service, '_activity_mode_owner', 'act_emergency')
    order = []
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_cleanup():
        assert router.get_activity_mode_owner() == 'act_emergency'
        order.append('cleanup')
        entered.set()
        await release.wait()
    monkeypatch.setattr(activity, 'user_intervention', delayed_cleanup)
    monkeypatch.setattr(robot, 'stop_velocity', lambda: order.append('zero'))
    monkeypatch.setattr(navigation.navigator, 'stop', lambda: order.append('navigation'))
    monkeypatch.setattr(router, '_configure_vision_consumers', lambda _mode: order.append('mode'))
    monkeypatch.setattr(gateway, 'cancel_pending', lambda **_kwargs: order.append('peers'))
    app = FastAPI()
    app.include_router(router.router)
    async def verified_context():
        return user
    app.dependency_overrides[router.require_service_context] = verified_context
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            result = await asyncio.wait_for(client.post('/api/go2/stop'), timeout=.5)
        assert result.status_code == 200
        assert result.json() == {'ok': True, 'result': 'accepted', 'mode': 'manual', 'physical_stop': 'unknown'}
        assert order[0] == 'zero'
        await asyncio.wait_for(entered.wait(), timeout=.5)
        assert not release.is_set()
        robot.send_command.assert_not_awaited()
    finally:
        cleanup_tasks = tuple(router.session_tasks._tasks.get(user.session_id, ()))
        release.set()
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks)
        assert router.get_activity_mode_owner() is None


@pytest.mark.asyncio
async def test_http_velocity_packets_never_wait_for_global_activity_teardown(device, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from cloud.go2 import router
    from cloud.api import activity
    robot, lease = device
    user = ExecutionContext('joystick-user', 'visitor', False, expires_at=int(time.time()) + 60)
    never = asyncio.Event()
    intervention = AsyncMock(side_effect=never.wait)
    monkeypatch.setattr(activity, 'user_intervention', intervention)
    move = MagicMock(return_value={'result': 'accepted', 'confirmation_scope': 'request'})
    monkeypatch.setattr(robot, 'move_velocity', move)
    lease.acquire(user)
    app = FastAPI()
    app.include_router(router.router)
    async def verified_context():
        return user
    app.dependency_overrides[router.require_service_context] = verified_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for velocity in ({'vx': .1, 'vy': 0, 'vyaw': .2}, {'vx': 0, 'vy': 0, 'vyaw': 0}):
            result = await asyncio.wait_for(client.post('/api/go2/velocity', json=velocity), timeout=.5)
            assert result.status_code == 200
    intervention.assert_not_awaited()
    assert move.call_args_list[0].args == (.1, 0, .2)
    assert move.call_args_list[1].args == (0, 0, 0)
