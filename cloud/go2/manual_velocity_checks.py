"""Manual control regressions; no network or physical robot is used."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from cloud.go2.connection.manual_velocity import slew_velocity
from cloud.go2.move_control_checks import robot_at_stand
from cloud.space.execution import ExecutionContext


def test_ramp_brakes_before_reversing_and_zero_axes_stop_immediately():
    assert slew_velocity((0, 0, 0), (.5, -.5, .8), .1) == pytest.approx((.06, -.06, .12))
    assert slew_velocity((.2, -.2, .3), (-.5, .5, -.8), .1) == pytest.approx((.14, -.14, .18))
    assert slew_velocity((.01, -.01, .01), (-.5, .5, -.8), .1) == pytest.approx((0, 0, 0))
    assert slew_velocity((.5, .5, .8), (0, .1, 0), .1) == pytest.approx((0, .44, 0))


@pytest.mark.asyncio
async def test_manual_targets_stream_without_more_http_or_state_publications():
    robot, _, packets = robot_at_stand()
    robot._move_control_active = True
    robot.manual_velocity(.5, 0, .8, can_continue=lambda: True)
    changed = robot.runtime.data['revision']
    try:
        await asyncio.sleep(.12)
        assert packets.call_count >= 3
        assert robot.runtime.data['revision'] == changed
        assert all(call.kwargs['data']['header']['identity']['api_id'] == 1008
                   for call in packets.call_args_list)
        assert 0 < robot._manual_velocity_current[0] < .5
        assert 0 < robot._manual_velocity_current[2] < .8
        robot.manual_velocity(0, 0, 0, can_continue=lambda: True)
        assert packets.call_args.kwargs['data']['header']['identity']['api_id'] == 1003
        count = packets.call_count
        await asyncio.sleep(.05)
        assert packets.call_count == count
        assert robot._manual_velocity_connection is None
    finally:
        robot.move_velocity(0, 0, 0)


@pytest.mark.asyncio
async def test_hardware_ticks_cannot_renew_the_input_watchdog():
    robot, _, packets = robot_at_stand()
    robot._move_control_active = True
    robot.manual_velocity(.3, 0, 0, can_continue=lambda: True, deadman_s=.07)
    await asyncio.sleep(.12)
    assert packets.call_args.kwargs['data']['header']['identity']['api_id'] == 1003
    assert robot._manual_velocity_handle is None
    assert robot._velocity_command_active is False


@pytest.mark.asyncio
@pytest.mark.parametrize('loss', ['ownership', 'posture', 'body_action', 'remote'])
async def test_stream_stops_when_its_authority_or_body_evidence_is_lost(loss):
    robot, _, packets = robot_at_stand()
    robot._move_control_active = True
    allowed = [True]
    robot.manual_velocity(.3, 0, 0, can_continue=lambda: allowed[0])
    if loss == 'ownership':
        allowed[0] = False
    elif loss == 'posture':
        robot._sport_state_observed_at = time.monotonic() - 10
    elif loss == 'body_action':
        await robot._command_lock.acquire()
    else:
        robot._move_control_active = False
    await asyncio.sleep(.07)
    assert packets.call_args.kwargs['data']['header']['identity']['api_id'] == 1003
    assert robot._manual_velocity_handle is None
    assert not robot._velocity_command_active
    if robot._command_lock.locked():
        robot._command_lock.release()


@pytest.mark.asyncio
async def test_navigation_override_and_reconnection_cannot_inherit_a_manual_stream():
    robot, _, packets = robot_at_stand()
    robot._move_control_active = True
    robot.manual_velocity(.3, 0, 0, can_continue=lambda: True)
    robot.move_velocity(.1, 0, .2, deadman_s=.3)
    count = packets.call_count
    await asyncio.sleep(.05)
    assert packets.call_count == count
    assert robot._manual_velocity_handle is None
    robot.move_velocity(0, 0, 0)
    robot.manual_velocity(.3, 0, 0, can_continue=lambda: True)
    robot._conn = object()  # A different connection must never receive old targets.
    count = packets.call_count
    await asyncio.sleep(.05)
    assert packets.call_count == count
    assert robot._manual_velocity_handle is None
    robot._velocity_timer.cancel()


@pytest.mark.asyncio
async def test_manual_input_requires_explicit_remote_entry():
    robot, _, packets = robot_at_stand()
    with pytest.raises(ValueError, match='请先进入移动控制'):
        robot.manual_velocity(.3, 0, 0, can_continue=lambda: True)
    packets.assert_not_called()


@pytest.mark.asyncio
async def test_intentional_disconnect_sends_stop_before_closing_the_transport():
    robot, _, packets = robot_at_stand()
    robot._move_control_active = True
    connection = robot._conn
    connection.disconnect = AsyncMock()
    robot.manual_velocity(.3, 0, 0, can_continue=lambda: True)
    async def disconnected():
        assert packets.call_args.kwargs['data']['header']['identity']['api_id'] == 1003
    connection.disconnect.side_effect = disconnected
    await robot.disconnect()
    connection.disconnect.assert_awaited_once()
    assert robot._manual_velocity_handle is None
    assert not robot.is_connected


@pytest.fixture
def velocity_endpoint(monkeypatch):
    from cloud.go2 import router
    robot, _, packets = robot_at_stand()
    robot._move_control_active = True
    context = ExecutionContext('manual-user', 'visitor', False, expires_at=int(time.time()) + 60)
    lease = SimpleNamespace(owner_session_id=context.session_id)
    monkeypatch.setattr(router, 'go2', robot)
    monkeypatch.setattr(router, 'control_lease', SimpleNamespace(snapshot=lambda: lease))
    check = MagicMock()
    monkeypatch.setattr(router, '_check_direct_motion', check)
    monkeypatch.setattr(router, '_cancel_pending_peer_requests', lambda _: None)
    monkeypatch.setattr(router, '_velocity_sequences', {})
    yield router, robot, packets, context, lease, check
    robot.move_velocity(0, 0, 0)


@pytest.mark.asyncio
async def test_late_or_duplicate_movement_cannot_override_release(velocity_endpoint):
    router, robot, packets, context, _, check = velocity_endpoint
    req = router.VelocityRequest
    await router.go2_velocity(req(vx=.3, controller_id='page', sequence=2), context)
    await router.go2_velocity(req(controller_id='page', sequence=4), context)
    count = packets.call_count
    for sequence in (1, 2, 3, 4):
        result = await router.go2_velocity(req(vx=.5, controller_id='page', sequence=sequence), context)
        assert result['ignored'] is True
    await asyncio.sleep(.05)
    assert packets.call_count == count
    assert check.call_count == 2  # Old writes cannot reacquire the motion lease.
    assert not robot._velocity_command_active
    await router.go2_velocity(req(vx=.1, controller_id='page', sequence=5), context)
    assert robot._velocity_command_active  # A deliberate new gesture is accepted.


@pytest.mark.asyncio
@pytest.mark.parametrize('loss', ['lease', 'session'])
async def test_http_stream_rechecks_lease_and_session_expiry(velocity_endpoint, loss):
    router, robot, packets, context, lease, _ = velocity_endpoint
    await router.go2_velocity(router.VelocityRequest(vx=.3), context)
    if loss == 'lease':
        lease.owner_session_id = 'new-owner'
    else:
        object.__setattr__(context, 'expires_at', int(time.time()) - 1)
    await asyncio.sleep(.07)
    assert packets.call_args.kwargs['data']['header']['identity']['api_id'] == 1003
    assert robot._manual_velocity_handle is None


@pytest.mark.asyncio
@pytest.mark.parametrize('params', [{'sequence': 1}, {'controller_id': 'page'}])
async def test_incomplete_ordering_metadata_is_rejected_before_motion(velocity_endpoint, params):
    router, _, packets, context, _, check = velocity_endpoint
    with pytest.raises(HTTPException) as exc:
        await router.go2_velocity(router.VelocityRequest(vx=.3, **params), context)
    assert exc.value.status_code == 400
    packets.assert_not_called()
    check.assert_not_called()
