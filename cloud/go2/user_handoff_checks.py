"""New trusted goals hand off only after old cleanup and measured stop proof."""
import asyncio
import importlib
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud.go2.connection.webrtc import Go2Connection
from cloud.go2.control import ControlLease, LeaseConflict, SessionTasks
from cloud.space.execution import ExecutionContext
from cloud.space.user_goals import get_goal_registry


@pytest.fixture
def handoff(monkeypatch, tmp_path):
    service = importlib.import_module('cloud.go2.service')
    control = importlib.import_module('cloud.go2.control')
    router = importlib.import_module('cloud.go2.router')
    nav = importlib.import_module('cloud.go2.navigation.navigator')
    monkeypatch.setenv('SSM_USER_GOALS_DIR', str(tmp_path / 'goals'))
    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    robot = Go2Connection()
    robot.is_connected = True
    robot.runtime.body('standing', confirmation='observed')
    robot._sport_state_observed_at = time.monotonic()
    robot._conn = SimpleNamespace(datachannel=SimpleNamespace(pub_sub=SimpleNamespace(
        publish_request_new=AsyncMock(), publish_without_callback=MagicMock())))
    lease, tasks = ControlLease(), SessionTasks()
    driver = SimpleNamespace(_task=None, owner_session_id=None, stop=MagicMock())
    navigator = SimpleNamespace(_task=None, owner_session_id=None, stop=MagicMock())
    monkeypatch.setattr(service, 'go2', robot)
    monkeypatch.setattr(service, 'drive', driver)
    monkeypatch.setattr(service, 'control_lease', lease)
    monkeypatch.setattr(service, '_motion_handoff_lock', asyncio.Lock())
    monkeypatch.setattr(service, '_prepared_goal_revision', 0)
    monkeypatch.setattr(service, 'person_teachings', SimpleNamespace(interrupt=MagicMock()))
    monkeypatch.setattr(control, 'session_tasks', tasks)
    monkeypatch.setattr(router, 'session_tasks', tasks)
    monkeypatch.setattr(nav, 'navigator', navigator)
    original = robot.stop_and_confirm_motion
    monkeypatch.setattr(robot, 'stop_and_confirm_motion', lambda **kw: original(timeout_s=.15, **kw))
    return SimpleNamespace(service=service, robot=robot, lease=lease, tasks=tasks,
                           drive=driver, nav=navigator)


def next_goal(session='user'):
    return ExecutionContext(session, 'visitor', False, expires_at=int(time.time())+60,
        goal_revision=get_goal_registry().advance(session))


def stationary(robot, count=3, **extra):
    for _ in range(count):
        robot._on_state({'data': {'velocity': [0, 0, 0], 'yaw_speed': 0, **extra}})


async def fresh_stop_samples(h):
    while not hasattr(h.robot, '_motion_stationary_samples'):
        await asyncio.sleep(.005)
    stationary(h.robot)


@pytest.mark.asyncio
async def test_new_goal_waits_old_finally_and_then_fresh_stop_before_release(handoff):
    h = handoff
    old_context = ExecutionContext('old-user', 'visitor', False)
    old_lease = h.lease.acquire(old_context)
    h.robot._move_control_active = True
    h.robot.manual_velocity(.2, 0, 0, can_continue=lambda: True)
    cleanup, finish = asyncio.Event(), asyncio.Event()
    async def old_motion():
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await finish.wait()
            h.robot.move_velocity(0, 0, 0)
            assert h.lease.release('old-user', generation=old_lease.generation) is False
    old = h.tasks.track('old-user', asyncio.create_task(old_motion()))
    await asyncio.sleep(0)
    context = next_goal()
    new = asyncio.create_task(h.service.prepare_user_goal(context))
    await cleanup.wait()
    assert h.robot._manual_velocity_handle is None
    assert not new.done()
    with pytest.raises(LeaseConflict):
        h.lease.acquire(context)
    finish.set()
    await asyncio.sleep(.02)
    assert old.done() and not new.done()
    stationary(h.robot)
    result = await new
    assert result['physical_stop'] == 'confirmed'
    assert h.lease.snapshot() is None
    current = h.lease.acquire(context)
    assert h.lease.release(context.session_id, generation=old_lease.generation) is False
    assert h.lease.snapshot() == current
    # Old cleanup is fully drained before any new stream can start.
    h.robot._move_control_active = True
    h.robot.manual_velocity(.1, 0, 0, can_continue=lambda: h.lease.allows(context.session_id, current.generation))
    await asyncio.sleep(.05)
    assert h.robot._velocity_command_active
    h.robot.stop_velocity()


@pytest.mark.asyncio
@pytest.mark.parametrize('telemetry', [None, {'yaw_speed': .3}, {'yaw_speed': None}, {'velocity': [.1, 0, 0]}])
async def test_accepted_standing_and_old_samples_do_not_release_control(handoff, telemetry):
    h = handoff
    old = h.lease.acquire(ExecutionContext('old-user', 'visitor', False))
    stationary(h.robot)  # Cached stationary telemetry predates this stop.
    task = asyncio.create_task(h.service.prepare_user_goal(next_goal()))
    await asyncio.sleep(.02)
    if telemetry is not None:
        stationary(h.robot, **telemetry)
    result = await task
    assert result['physical_stop'] == 'unknown'
    assert h.lease.snapshot().generation == old.generation
    assert not h.lease.release('old-user', generation=old.generation)
    # Waiting for the old TTL cannot manufacture completion.
    with pytest.raises(LeaseConflict):
        h.lease.acquire(ExecutionContext('next', 'admin', True), now=time.time()+1000)


@pytest.mark.asyncio
async def test_unknown_stop_can_be_confirmed_by_a_later_goal_without_waiting_ttl(handoff):
    h = handoff
    h.lease.acquire(ExecutionContext('old', 'visitor', False))
    first = await h.service.prepare_user_goal(next_goal())
    assert first['physical_stop'] == 'unknown'
    task = asyncio.create_task(h.service.prepare_user_goal(next_goal('next')))
    await asyncio.sleep(.02)
    stationary(h.robot)
    assert (await task)['physical_stop'] == 'confirmed'
    assert h.lease.snapshot() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('posture', ['sitting', 'lying'])
async def test_zero_velocity_can_confirm_stop_without_claiming_standing(handoff, posture):
    h = handoff
    h.robot.runtime.body(posture, confirmation='observed')
    h.lease.acquire(ExecutionContext('old', 'visitor', False))
    task = asyncio.create_task(h.service.prepare_user_goal(next_goal()))
    await asyncio.sleep(.02)
    stationary(h.robot)
    assert (await task)['physical_stop'] == 'confirmed'
    assert h.robot.body_state == posture


@pytest.mark.asyncio
async def test_superseded_handoff_cannot_release_lease_or_dispatch_again(handoff):
    h = handoff
    old = h.lease.acquire(ExecutionContext('old', 'visitor', False))
    context = next_goal()
    task = asyncio.create_task(h.service.prepare_user_goal(context))
    await asyncio.sleep(.02)
    next_goal('newer')
    packets = h.robot._conn.datachannel.pub_sub.publish_without_callback.call_count
    result = await task
    assert result['physical_stop'] == 'unknown'
    assert h.lease.snapshot().generation == old.generation
    assert h.robot._conn.datachannel.pub_sub.publish_without_callback.call_count == packets


@pytest.mark.asyncio
async def test_duplicate_handoff_does_not_stop_new_same_session_control(handoff):
    h = handoff
    context = next_goal()
    old = h.lease.acquire(ExecutionContext('old', 'visitor', False))
    task = asyncio.create_task(h.service.prepare_user_goal(context))
    await asyncio.sleep(.02)
    stationary(h.robot)
    assert (await task)['physical_stop'] == 'confirmed'
    new = h.lease.acquire(context)
    assert new.generation != old.generation
    assert not h.lease.release('old', generation=old.generation, stop_confirmed=True)
    packets = h.robot._conn.datachannel.pub_sub.publish_without_callback.call_count
    assert (await h.service.prepare_user_goal(context))['physical_stop'] == 'not_required'
    assert h.lease.snapshot() == new
    assert h.robot._conn.datachannel.pub_sub.publish_without_callback.call_count == packets


@pytest.mark.asyncio
async def test_emergency_stop_remains_available_while_handoff_is_unconfirmed(handoff, monkeypatch):
    h = handoff
    router = importlib.import_module('cloud.go2.router')
    gateway = importlib.import_module('cloud.peers.gateway')
    h.lease.acquire(ExecutionContext('old', 'visitor', False))
    assert (await h.service.prepare_user_goal(next_goal()))['physical_stop'] == 'unknown'
    monkeypatch.setattr(router, 'go2', h.robot)
    monkeypatch.setattr(router, 'control_lease', h.lease)
    monkeypatch.setattr(router, 'session_tasks', h.tasks)
    monkeypatch.setattr(router, '_configure_vision_consumers', MagicMock())
    monkeypatch.setattr(gateway, 'cancel_pending', MagicMock())
    packets = h.robot._conn.datachannel.pub_sub.publish_without_callback.call_count
    result = await router.emergency_stop(ExecutionContext('user', 'visitor', False))
    assert result['result'] == 'accepted' and result['physical_stop'] == 'unknown'
    assert h.robot._conn.datachannel.pub_sub.publish_without_callback.call_count > packets
    assert h.lease.snapshot() is not None


@pytest.mark.asyncio
async def test_first_velocity_goal_confirms_stop_and_preserves_explicit_remote_entry(handoff, monkeypatch):
    h = handoff
    router = importlib.import_module('cloud.go2.router')
    executor = importlib.import_module('cloud.go2.tools.executor')
    h.lease.acquire(ExecutionContext('user', 'visitor', False))
    h.robot._move_control_active = True  # An explicit prior EnterMoveControl.
    monkeypatch.setattr(router, 'go2', h.robot)
    monkeypatch.setattr(router, 'control_lease', h.lease)
    monkeypatch.setattr(router, '_velocity_sequences', {})
    monkeypatch.setattr(router, '_cancel_pending_peer_requests', lambda _: None)
    monkeypatch.setattr(executor, 'go2', h.robot)
    monkeypatch.setattr(executor, 'control_lease', h.lease)
    context = next_goal()
    first = asyncio.create_task(router.go2_velocity(router.VelocityRequest(
        vx=.1, controller_id='gesture', sequence=1), context))
    await asyncio.sleep(.02)
    assert not first.done()
    stationary(h.robot)
    assert (await first)['result'] == 'accepted'
    assert h.robot._move_control_active and h.robot._velocity_command_active
    h.robot.stop_velocity()


@pytest.mark.asyncio
async def test_in_goal_child_preflight_does_not_cancel_its_parent_graph(handoff):
    h = handoff
    context = next_goal()
    previous = h.lease.acquire(context)
    parent = asyncio.current_task()
    h.tasks.track(context.session_id, parent)
    child = asyncio.create_task(h.service.prepare_user_interaction(context))
    await asyncio.wait_for(child, timeout=.5)
    assert not parent.cancelling()
    assert h.lease.snapshot() == previous
    h.robot._conn.datachannel.pub_sub.publish_without_callback.assert_not_called()


@pytest.mark.asyncio
async def test_late_old_task_stop_cannot_stop_a_new_same_session_velocity(handoff, monkeypatch):
    h = handoff
    router = importlib.import_module('cloud.go2.router')
    previous = next_goal()
    current = next_goal()
    lease = h.lease.acquire(current)
    h.robot._move_control_active = True
    h.robot.manual_velocity(.1, 0, 0,
        can_continue=lambda: h.lease.allows(current.session_id, lease.generation))
    monkeypatch.setattr(router, 'go2', h.robot)
    monkeypatch.setattr(router, 'control_lease', h.lease)
    packets = h.robot._conn.datachannel.pub_sub.publish_without_callback.call_count
    assert (await router._stop_task_motion(previous))['result'] == 'cancelled'
    assert h.robot._conn.datachannel.pub_sub.publish_without_callback.call_count == packets
    assert h.robot._velocity_command_active
    h.robot.stop_velocity()
