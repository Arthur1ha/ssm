"""Shared motion dynamics and planner/control scheduling, without hardware."""

import asyncio
import math
import threading
from types import SimpleNamespace

import pytest

from cloud.go2.motion import (
    ANGULAR_ACCELERATION, COMMAND_TTL_S, LINEAR_ACCELERATION,
    VelocityProfile, slew_velocity, turn_velocity,
)


def test_replayed_delivery_axis_changes_have_bounded_vector_acceleration():
    # The real delivery alternated forward/left commands before a -.8 yaw turn.
    targets = [(0.25, 0, 0)] * 8 + [(0, .25, .008), (.25, 0, -.006)] * 8 + [(0, 0, -.8)] * 8
    previous = (0, 0, 0)
    for target in targets:
        current = slew_velocity(previous, target, .1)
        assert math.hypot(current[0]-previous[0], current[1]-previous[1]) <= LINEAR_ACCELERATION*.1 + 1e-9
        assert abs(current[2]-previous[2]) <= ANGULAR_ACCELERATION*.1 + 1e-9
        assert math.hypot(*current[:2]) <= .250001
        previous = current


def test_no_axis_chatter_when_direction_wobbles_around_45_degrees():
    current = (0, 0, 0)
    for angle in [math.pi/4 + offset for offset in (-.01, .01)] * 30:
        current = slew_velocity(current, (.25*math.cos(angle), .25*math.sin(angle), 0), .1)
        assert current[0] > 0 and current[1] > 0
        assert math.atan2(current[1], current[0]) == pytest.approx(math.pi/4, abs=.011)


def test_pause_or_watchdog_expiry_resets_ramp_instead_of_resuming_old_speed():
    profile = VelocityProfile()
    profile.sent((.5, 0, .8), 10.)
    assert profile.preview((.5, 0, .8), 10. + COMMAND_TTL_S + .01) == pytest.approx((.06, 0, .12))
    profile.reset()
    assert profile.preview((0, .3, 0), 30.) == pytest.approx((0, .06, 0))
    assert profile.velocity == (0, 0, 0)  # An unvalidated preview never becomes sent state.


def test_turn_profile_brakes_without_exceeding_requested_speed():
    speeds = [turn_velocity(angle, .8, .05) for angle in (1., .3, .15, .06)]
    assert speeds == sorted(speeds, reverse=True)
    assert speeds[0] == .8 and speeds[-1] < .8
    assert turn_velocity(-.1, .2, .05) == -.2
    assert slew_velocity((.3, .2, .7), (0, 0, 0), .01) == (0, 0, 0)


@pytest.mark.asyncio
async def test_one_duplicate_pose_withholds_move_without_inserting_a_stop(monkeypatch):
    from cloud.go2.navigation import navigator as module
    from cloud.go2.tests.test_target_navigation_regressions import control_simulation

    robot, commands, duplicates = [None], [], []
    def receive(pose, now):
        active = len([cmd for cmd in commands if any(cmd)])
        if active == 2 and not duplicates:
            duplicates.append(True)
            return
        robot[0].odom_received_at = now
    nav, _, _, captured = control_simulation(monkeypatch, goal=(.4, 0.),
        on_tick=receive, effective_motion=True)
    commands = captured
    robot[0] = SimpleNamespace(**vars(module.go2), odom_received_at=98.)
    monkeypatch.setattr(module, 'go2', robot[0])
    result = await nav.go_to_coordinate(.4, 0., holonomic=True, arrival_tolerance_m=.08,
                                        minimum_command_speed=.25, speed_limit=.3)
    assert result['status'] == 'succeeded' and duplicates, result
    assert commands[0] == commands[-1] == (0, 0, 0)
    assert all(any(cmd) for cmd in commands[1:-1])


@pytest.mark.asyncio
async def test_periodic_planning_does_not_block_validated_motion(monkeypatch):
    from cloud.go2.navigation import navigator as module
    from cloud.go2.tests.test_target_navigation_regressions import StreamingRobot

    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 's'}
    commands, plans = [], []
    planning, release = threading.Event(), threading.Event()
    grid = SimpleNamespace(grid_to_odom=lambda x, y: (float(x), float(y)),
                           is_body_clear=lambda *_: True)
    def move(x, y, yaw, **kwargs):
        commands.append((x, y, yaw))
        pose['x'] += x*.1
    def plan(*_):
        plans.append(True)
        if len(plans) > 1:
            planning.set()
            release.wait(2.)
        return [(i, 0) for i in range(11)], grid, (10., 0., False)

    monkeypatch.setattr(module, 'go2', StreamingRobot(
        _odom_session_id='s', is_connected=True, move_velocity=move))
    monkeypatch.setattr(module, 'REPLAN_INTERVAL', .05)
    nav = module.Navigator()
    monkeypatch.setattr(nav, '_snapshot', lambda: ((pose.copy(), grid), None))
    monkeypatch.setattr(nav, '_plan_path', plan)
    task = asyncio.create_task(nav.go_to_coordinate(10., 0., holonomic=True))
    try:
        async with asyncio.timeout(2):
            while not planning.is_set():
                await asyncio.sleep(.01)
            before = len(commands)
            await asyncio.sleep(.25)
            assert len(commands) >= before + 2
            assert all(any(cmd) for cmd in commands[1:])  # _begin first stops prior work.
    finally:
        task.cancel()
        await task
        release.set()
    assert commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
async def test_smoothed_momentum_is_checked_against_live_obstacles(monkeypatch):
    from cloud.go2.navigation import navigator as module
    from cloud.go2.tests.test_target_navigation_regressions import control_simulation

    nav, _, _, commands = control_simulation(monkeypatch, goal=(1., 0.))
    snapshot = nav._snapshot
    def live_snapshot():
        (pose, grid), reason = snapshot()
        # After moving forward, ask for pure yaw. A forward remnant from the
        # smoothing ramp would enter an obstacle, although pure yaw is clear.
        if any(any(cmd) for cmd in commands):
            pose['x'] = 1.
            pose['heading'] = 0.
            grid.is_body_clear = lambda x, y, heading: x <= 1.
        return (pose, grid), reason
    monkeypatch.setattr(nav, '_snapshot', live_snapshot)
    result = await nav.go_to_coordinate(1., 0., heading=.5, holonomic=True)
    assert result['status'] == 'blocked'
    assert result['reason'] == 'body_clearance_unavailable'
    assert len([cmd for cmd in commands if any(cmd)]) == 1
    assert commands[-1] == (0, 0, 0)
