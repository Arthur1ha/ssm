"""Cargo matching regressions without live robot I/O or extra motion/time cutoffs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cloud.go2.tests.test_relative_move import movement


@pytest.mark.asyncio
@pytest.mark.parametrize('delay,displacement,rotation', [
    (.4, 0., 0.), (0., .04, 0.), (0., 0., .06), (.4, .04, .06),
])
async def test_matching_accepts_elapsed_time_and_motion_with_live_pose(
        movement, monkeypatch, tmp_path, delay, displacement, rotation):
    from cloud.go2.navigation import navigator as module

    monkeypatch.setenv('SSM_USER_GOALS_DIR', str(tmp_path / 'goals'))
    h = movement
    pose, grid = h.robot.odom, h.robot.occupancy_grid
    goal = {**pose, 'x': pose['x'] + displacement,
            'heading': pose['heading'] + rotation}
    clock = module.time.monotonic
    elapsed = [0.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock() + elapsed[0])

    def match(current_grid, captured_pose):
        if contact.grid_for.call_count > 1:
            assert plan.called, 'Successful matching was discarded before planning'
        elapsed[0] += delay
        pose.update(goal)
        # Sensor packets remain fresh while matching runs off the event loop.
        h.robot.odom_received_at = h.robot.voxel_received_at = module.time.monotonic()
        grid.observed_at = module.time.monotonic()
        return current_grid

    contact = SimpleNamespace(context=h.context, grid_for=Mock(side_effect=match))
    plan = Mock(return_value=([(0, 0)], grid, (goal['x'], goal['y'], False)))
    monkeypatch.setattr(h.nav, '_plan_path', plan)
    monkeypatch.setattr(h.nav, '_confirm_stopped', AsyncMock(return_value=True))
    module.control_lease.acquire(h.context)

    result = await h.nav.go_to_coordinate(
        goal['x'], goal['y'], owner_session_id=h.context.session_id,
        cargo_contact=contact)

    assert result['status'] == 'succeeded', result
    assert contact.grid_for.call_count >= 1
    assert plan.call_args_list[0].args[2] == goal
    assert result['last_trusted_pose'] == goal
    assert not any(any(command) for command in h.commands)
