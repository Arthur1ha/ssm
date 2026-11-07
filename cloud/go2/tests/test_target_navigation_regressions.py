"""Navigation regressions retained from the former RGB/LiDAR executor."""
import math
from types import SimpleNamespace
import numpy as np
import pytest

@pytest.mark.asyncio
async def test_visual_path_translates_sideways_when_turn_disk_has_unknown_floor(monkeypatch):
    from cloud.go2.navigation import navigator as nav_module

    pose = {"x": 0.0, "y": 0.0, "heading": 0.38, "session_id": "s"}
    commands = []

    def move(vx, vy, yaw, **_):
        commands.append((vx, vy, yaw))
        if abs(vx) + abs(vy) > 0:
            pose.update(x=1.0, y=-0.5)
        return {"result": "accepted"}

    grid = SimpleNamespace(
        grid_to_odom=lambda ix, iy: (0.0, 0.0) if ix == 0 else (0.5, -0.3),
        is_body_clear=lambda *args: True,
        is_turn_clear=lambda *args: False,
    )
    monkeypatch.setattr(nav_module, "go2", SimpleNamespace(
        _odom_session_id="s", move_velocity=move))
    nav = nav_module.Navigator()
    monkeypatch.setattr(nav, "_snapshot", lambda: ((pose.copy(), grid), None))
    monkeypatch.setattr(nav, "_plan_path", lambda *args: (
        [(0, 0), (1, 0)], grid, (1.0, -0.5, False)))

    async def stopped(*_):
        return True

    monkeypatch.setattr(nav, "_confirm_stopped", stopped)
    result = await nav.go_to_coordinate(1.0, -0.5, speed_limit=0.15,
                                        holonomic=True, face_target=(2.0, -0.5))
    assert result["status"] == "succeeded"
    active = [command for command in commands if any(command)]
    assert active and active[0][0] > 0 and active[0][1] < 0
    assert active[0][2] < 0  # translate and turn toward the target together


def test_visual_replan_selects_reachable_stop_when_original_cell_disappears():
    from cloud.go2.navigation.navigator import Navigator

    blocked = np.zeros((20, 20), dtype=bool)
    blocked[10, 14] = True  # the original stop has become occupied on the live map
    grid = SimpleNamespace(
        grid=blocked, resolution=1.0, has_free_space_evidence=True,
        odom_to_grid=lambda x, y: (int(round(x + 10)), int(round(y + 10))),
        grid_to_odom=lambda x, y: (float(x - 10), float(y - 10)),
        is_free=lambda x, y: (0 <= x < 20 and 0 <= y < 20 and not blocked[y, x]),
        is_body_clear=lambda *args: True,
    )
    pose = {"x": 2.0, "y": 0.0, "heading": 0.0}
    result = Navigator()._plan_path(
        {"x": 4.0, "y": 0.0, "keepout": (5.0, 0.0, 0.5),
         "face_target": (5.0, 0.0)}, grid, pose)
    assert result is not None
    path, _, goal = result
    assert path and goal[2] is True
    assert goal[:2] != (4.0, 0.0)
    assert all(grid.is_free(x, y) for x, y in path)


def test_lookahead_does_not_reverse_toward_old_path_start():
    from cloud.go2.navigation.navigator import _find_lookahead_on_path

    grid = SimpleNamespace(grid_to_odom=lambda ix, iy: (ix * 0.05, iy * 0.05))
    path = [(ix, 0) for ix in range(41)]
    lookahead = _find_lookahead_on_path(
        path, grid, robot_x=0.82, robot_y=0.0, robot_heading=0.0,
        goal_loc={"x": 2.0, "y": 0.0}, front_only=False)
    assert lookahead[0] > 0.82


def test_replan_routes_around_measured_body_clearance_failure():
    from cloud.go2.navigation.navigator import Navigator

    blocked = np.zeros((24, 24), dtype=bool)
    grid = SimpleNamespace(
        grid=blocked, resolution=1.0,
        odom_to_grid=lambda x, y: (int(round(x + 10)), int(round(y + 10))),
        grid_to_odom=lambda x, y: (float(x - 10), float(y - 10)),
        is_free=lambda x, y: 0 <= x < 24 and 0 <= y < 24,
    )
    pose = {"x": 0.0, "y": 0.0}
    loc = {"x": 6.0, "y": 0.0}
    direct, _, _ = Navigator()._plan_path(loc, grid, pose)
    rerouted, _, _ = Navigator()._plan_path(loc, grid, pose, [(3.0, 0.0)])
    assert (13, 10) in direct
    assert (13, 10) not in rerouted
    assert rerouted[0] == direct[0] and rerouted[-1] == direct[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("body_clear", [True, False])
async def test_game_stop_cell_tolerance_checks_clearance_and_stillness(
        monkeypatch, body_clear):
    from cloud.go2.navigation import navigator as nav_module

    pose = {"x": 0.45, "y": 4.50, "heading": 1.8, "session_id": "s"}
    commands = []
    grid = SimpleNamespace(is_body_clear=lambda *_: body_clear)
    monkeypatch.setattr(nav_module, "go2", SimpleNamespace(
        _odom_session_id="s", move_velocity=lambda *args, **kwargs: commands.append(args)))
    nav = nav_module.Navigator()
    monkeypatch.setattr(nav, "_snapshot", lambda: ((pose.copy(), grid), None))
    monkeypatch.setattr(nav, "_plan_path", lambda *_: (
        [(0, 0)], grid, (0.5, 4.9, False)))
    confirmed = []

    async def confirm(*args):
        confirmed.append(args)
        return True

    monkeypatch.setattr(nav, "_confirm_stopped", confirm)
    result = await nav.go_to_coordinate(
        0.5, 4.9, holonomic=True, face_target=(0.125, 5.925),
        keepout=(0.125, 5.925, 0.39),
        stop_when_near_target_m=0.89, stop_cell_tolerance_m=0.45)

    assert result["status"] == ("succeeded" if body_clear else "blocked")
    assert bool(confirmed) is body_clear
    assert commands and all(command == (0, 0, 0) for command in commands)


@pytest.mark.asyncio
async def test_visual_navigation_stops_on_near_side_before_old_goal(monkeypatch):
    from cloud.go2.navigation import navigator as nav_module

    pose = {"x": 0.31, "y": 0.0, "heading": 0.0, "session_id": "s"}
    commands = []
    monkeypatch.setattr(nav_module, "go2", SimpleNamespace(
        _odom_session_id="s", move_velocity=lambda *args, **kwargs: commands.append(args)))
    nav = nav_module.Navigator()
    monkeypatch.setattr(nav, "_snapshot", lambda: ((pose.copy(), object()), None))
    monkeypatch.setattr(nav, "_plan_path", lambda *_: pytest.fail("should stop before replanning"))

    async def stopped(*_):
        return True

    monkeypatch.setattr(nav, "_confirm_stopped", stopped)
    result = await nav.go_to_coordinate(
        1.4, 0.5, holonomic=True, face_target=(1.0, 0.0),
        keepout=(1.0, 0.0, 0.39), stop_when_near_target_m=0.74)
    assert result["status"] == "succeeded"
    assert result["actual_goal"]["adjusted"] is True
    assert all(command == (0, 0, 0) for command in commands)


@pytest.mark.asyncio
async def test_visual_navigation_accepts_stop_cell_without_last_centimeters(monkeypatch):
    from cloud.go2.navigation import navigator as nav_module

    pose = {"x": 0.0, "y": 0.0, "heading": 0.0, "session_id": "s"}
    commands = []
    grid = object()
    monkeypatch.setattr(nav_module, "go2", SimpleNamespace(
        _odom_session_id="s", move_velocity=lambda *args, **kwargs: commands.append(args)))
    nav = nav_module.Navigator()
    monkeypatch.setattr(nav, "_snapshot", lambda: ((pose.copy(), grid), None))
    monkeypatch.setattr(nav, "_plan_path", lambda *_: (
        [(0, 0)], grid, (0.12, 0.0, False)))

    async def stopped(*_):
        return True

    monkeypatch.setattr(nav, "_confirm_stopped", stopped)
    result = await nav.go_to_coordinate(
        0.12, 0.0, holonomic=True, face_target=(1.0, 0.0),
        keepout=(1.0, 0.0, 0.39), stop_when_near_target_m=0.89)
    assert result["status"] == "succeeded"
    assert all(command == (0, 0, 0) for command in commands)


@pytest.mark.asyncio
async def test_navigation_stops_during_short_odom_gap_then_replans(monkeypatch):
    from cloud.go2.navigation import navigator as nav_module

    pose = {"x": 0.0, "y": 0.0, "heading": 0.38, "session_id": "s"}
    commands = []
    snapshots = 0

    def move(vx, vy, yaw, **_):
        commands.append((vx, vy, yaw))
        if abs(vx) + abs(vy) > 0:
            pose.update(x=1.0, y=-0.5)
        return {"result": "accepted"}

    grid = SimpleNamespace(
        grid_to_odom=lambda ix, iy: (0.0, 0.0) if ix == 0 else (0.5, -0.3),
        is_body_clear=lambda *args: True,
        is_turn_clear=lambda *args: False,
    )
    robot = SimpleNamespace(_odom_session_id="s", is_connected=True,
                            move_velocity=move, sensor_diagnostics={"pose": {"age_s": 1.2}})
    monkeypatch.setattr(nav_module, "go2", robot)
    nav = nav_module.Navigator()

    def snapshot():
        nonlocal snapshots
        snapshots += 1
        if snapshots <= 2:
            return None, "localization_unavailable"
        return (pose.copy(), grid), None

    monkeypatch.setattr(nav, "_snapshot", snapshot)
    monkeypatch.setattr(nav, "_plan_path", lambda *args: (
        [(0, 0), (1, 0)], grid, (1.0, -0.5, False)))

    async def stopped(*_):
        return True

    monkeypatch.setattr(nav, "_confirm_stopped", stopped)
    result = await nav.go_to_coordinate(1.0, -0.5, speed_limit=0.15,
                                        holonomic=True)
    assert result["status"] == "succeeded"
    assert snapshots >= 4
    assert commands[0] == (0, 0, 0)
    assert any(abs(vy) > 0 for _, vy, _ in commands)


@pytest.mark.asyncio
async def test_short_observation_move_requires_real_progress_before_arrival(monkeypatch):
    from cloud.go2.navigation import navigator as nav_module

    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 's'}
    commands = []
    def move(vx, vy, yaw, **_):
        commands.append((vx, vy, yaw))
        if vx > 0:
            pose['x'] += .06
    grid = SimpleNamespace(grid_to_odom=lambda ix, iy: (ix * .05, 0.),
                           is_body_clear=lambda *_: True)
    monkeypatch.setattr(nav_module, 'go2', SimpleNamespace(
        _odom_session_id='s', move_velocity=move))
    nav = nav_module.Navigator()
    monkeypatch.setattr(nav, '_snapshot', lambda: ((pose.copy(), grid), None))
    monkeypatch.setattr(nav, '_plan_path', lambda *_: (
        [(i, 0) for i in range(7)], grid, (.30, 0., False)))
    async def stopped(goal, heading, tolerance):
        assert tolerance == .08
        assert pose['x'] >= .22
        return True
    monkeypatch.setattr(nav, '_confirm_stopped', stopped)
    result = await nav.go_to_coordinate(.30, 0., holonomic=True,
                                        speed_limit=.15, arrival_tolerance_m=.08)
    assert result['status'] == 'succeeded'
    assert sum(vx > 0 for vx, _, _ in commands) >= 4


@pytest.mark.asyncio
async def test_unexecuted_motion_stops_without_waiting_for_navigation_timeout(monkeypatch):
    from cloud.go2.navigation import navigator as nav_module
    import time
    tick = [100.]
    async def sleep(_):
        tick[0] += .1
    monkeypatch.setattr(nav_module, 'time', SimpleNamespace(monotonic=lambda: tick[0], time=time.time))
    monkeypatch.setattr(nav_module, 'asyncio', SimpleNamespace(
        current_task=lambda: None, sleep=sleep, CancelledError=__import__('asyncio').CancelledError))
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 's'}
    commands = []
    grid = SimpleNamespace(grid_to_odom=lambda ix, iy: (ix * .05, 0.),
                           is_body_clear=lambda *_: True)
    monkeypatch.setattr(nav_module, 'go2', SimpleNamespace(
        _odom_session_id='s', move_velocity=lambda *args, **kwargs: commands.append(args)))
    nav = nav_module.Navigator()
    monkeypatch.setattr(nav, '_snapshot', lambda: ((pose.copy(), grid), None))
    monkeypatch.setattr(nav, '_plan_path', lambda *_: (
        [(i, 0) for i in range(7)], grid, (.30, 0., False)))
    result = await nav.go_to_coordinate(.30, 0., holonomic=True,
        speed_limit=.3, minimum_command_speed=.25, arrival_tolerance_m=.08)
    assert result['reason'] == 'motion_not_observed'
    assert tick[0] < 106.
    assert commands[-1] == (0, 0, 0)
    assert all(vx >= .25 for vx, vy, yaw in commands if vx)


def control_simulation(monkeypatch, *, goal=(.4, .4), on_tick=None, effective_motion=False):
    from cloud.go2.navigation import navigator as module
    import asyncio
    import time
    tick = [100.]
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 's'}
    commands = []
    async def sleep(_):
        tick[0] += .1
        if on_tick:
            on_tick(pose, tick[0])
    def move(vx, vy, yaw, **_):
        commands.append((vx, vy, yaw))
        if effective_motion:
            if abs(vx) >= .24:
                pose['x'] += vx * .1
            if abs(vy) >= .24:
                pose['y'] += vy * .1
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: tick[0], time=time.time))
    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(current_task=lambda: None,
                                                          sleep=sleep, CancelledError=asyncio.CancelledError))
    monkeypatch.setattr(module, 'go2', SimpleNamespace(_odom_session_id='s', move_velocity=move))
    grid = SimpleNamespace(grid_to_odom=lambda ix, iy: (ix * goal[0], iy * goal[1]),
                           is_body_clear=lambda *_: True, turn_sweep_details=lambda *_: {'clear': True})
    nav = module.Navigator()
    monkeypatch.setattr(nav, '_snapshot', lambda: ((pose.copy(), grid), None))
    monkeypatch.setattr(nav, '_plan_path', lambda *_: ([(0, 0), (1, 1)], grid, (*goal, False)))
    async def stopped(*_):
        return True
    monkeypatch.setattr(nav, '_confirm_stopped', stopped)
    return nav, tick, pose, commands


@pytest.mark.asyncio
async def test_diagonal_floor_moves_effective_axes_without_increasing_norm(monkeypatch):
    nav, tick, pose, commands = control_simulation(monkeypatch, effective_motion=True)
    result = await nav.go_to_coordinate(.4, .4, holonomic=True, arrival_tolerance_m=.08,
                                        minimum_command_speed=.25, speed_limit=.3)
    assert result['status'] == 'succeeded'
    assert pose['x'] > .3 and pose['y'] > .3
    for vx, vy, _ in commands:
        assert math.hypot(vx, vy) <= .300001
        assert all(abs(axis) >= .25 for axis in (vx, vy) if axis)


@pytest.mark.asyncio
async def test_heading_wobble_cannot_hide_failed_translation(monkeypatch):
    nav, tick, _, commands = control_simulation(monkeypatch,
        on_tick=lambda pose, now: pose.update(heading=.06 * math.sin(now)))
    result = await nav.go_to_coordinate(.4, .4, holonomic=True, face_target=(2., 0.),
                                        minimum_command_speed=.25, speed_limit=.3)
    assert result['reason'] == 'motion_not_observed'
    assert result['diagnostics']['motion_phase'] == 'translation'
    assert tick[0] < 106.
    assert commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
async def test_final_heading_alignment_also_has_progress_deadline(monkeypatch):
    nav, tick, _, commands = control_simulation(monkeypatch, goal=(0., 0.))
    result = await nav.go_to_coordinate(0., 0., heading=1.)
    assert result['reason'] == 'motion_not_observed'
    assert result['diagnostics']['motion_phase'] == 'rotation'
    assert tick[0] < 106.
    assert any(yaw for _, _, yaw in commands)
    assert commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
async def test_alternating_turn_and_translation_cannot_reset_stall_timer(monkeypatch):
    nav, tick, pose, commands = control_simulation(monkeypatch, goal=(.4, 0.),
        on_tick=lambda pose, now: pose.update(heading=-.49 if pose['heading'] < -.5 else -.51))
    pose['heading'] = -.49
    result = await nav.go_to_coordinate(.4, 0., arrival_tolerance_m=.08,
                                        minimum_command_speed=.25, speed_limit=.3)
    assert result['reason'] == 'motion_not_observed'
    assert tick[0] < 106.
    assert any(vx for vx, _, _ in commands)
    assert any(yaw and not vx for vx, _, yaw in commands)
    assert commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
async def test_stale_pose_after_slow_planning_never_dispatches_motion(monkeypatch):
    from cloud.go2.navigation import navigator as module
    nav, tick, _, commands = control_simulation(monkeypatch)
    snapshot = nav._snapshot
    def slow_plan(*args):
        tick[0] += 1.2
        return [(0, 0), (1, 1)], snapshot()[0][1], (.4, .4, False)
    monkeypatch.setattr(nav, '_plan_path', slow_plan)
    monkeypatch.setattr(nav, '_snapshot', lambda: snapshot() if tick[0] <= 101.
                        else (None, 'localization_unavailable'))
    result = await nav.go_to_coordinate(.4, .4, holonomic=True)
    assert result['reason'] == 'localization_unavailable'
    assert commands and all(command == (0, 0, 0) for command in commands)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['session', 'lease', 'keepout'])
async def test_planning_revalidates_session_control_and_keepout(monkeypatch, change):
    from cloud.go2.navigation import navigator as module
    from cloud.go2.control import LeaseConflict
    nav, _, pose, commands = control_simulation(monkeypatch, goal=(.4, 0.))
    plan = nav._plan_path
    lost = [False]
    def heartbeat(_):
        if lost[0]:
            raise LeaseConflict('other-owner', 9999999999.)
    monkeypatch.setattr(module, 'control_lease', SimpleNamespace(heartbeat_session=heartbeat))
    def changed_plan(*args):
        result = plan(*args)
        if change == 'session':
            module.go2._odom_session_id = 'new-session'
        elif change == 'lease':
            lost[0] = True
        else:
            pose['x'] = .02  # cross the keepout edge without triggering a pose-jump check
        return result
    monkeypatch.setattr(nav, '_plan_path', changed_plan)
    result = await nav.go_to_coordinate(.4, 0., owner_session_id='owner',
                                       keepout=(.51, 0., .5) if change == 'keepout' else None)
    expected = {'session': 'map_mismatch', 'lease': 'control_lease_lost', 'keepout': 'target_keepout_breached'}
    assert result['reason'] == expected[change]
    assert commands and all(command == (0, 0, 0) for command in commands)


def grid_for_precise_move(*, only_start_free=False):
    blocked = np.ones((40, 40), dtype=bool) if only_start_free else np.zeros((40, 40), dtype=bool)
    blocked[20, 20] = False
    return SimpleNamespace(grid=blocked, resolution=.05, origin=(-1., -1.), width=(40, 40),
        odom_to_grid=lambda x, y: (math.floor((x+1.)/.05), math.floor((y+1.)/.05)),
        grid_to_odom=lambda ix, iy: (-1.+(ix+.5)*.05, -1.+(iy+.5)*.05),
        is_free=lambda ix, iy: 0 <= ix < 40 and 0 <= iy < 40 and not blocked[iy, ix],
        is_body_clear=lambda x, y, heading: abs(heading) < .01)


def test_precision_move_cannot_replace_blocked_goal_with_current_cell():
    from cloud.go2.navigation.navigator import Navigator
    grid = grid_for_precise_move(only_start_free=True)
    pose = {'x': .025, 'y': .025, 'heading': 0.}
    assert Navigator()._plan_path({'x': .325, 'y': .025, 'arrival_tolerance_m': .08}, grid, pose) is None


@pytest.mark.asyncio
async def test_forward_goal_retains_coordinate_without_grid_induced_turn(monkeypatch):
    from cloud.go2.navigation import navigator as module
    nav, _, pose, commands = control_simulation(monkeypatch, goal=(.30, 0.), effective_motion=True)
    grid = grid_for_precise_move()
    monkeypatch.setattr(nav, '_snapshot', lambda: ((pose.copy(), grid), None))
    monkeypatch.setattr(nav, '_plan_path', module.Navigator._plan_path.__get__(nav))
    result = await nav.go_to_coordinate(.30, 0., arrival_tolerance_m=.08,
                                        minimum_command_speed=.25, speed_limit=.3)
    assert result['status'] == 'succeeded'
    assert result['actual_goal'] == {'x': .30, 'y': 0., 'adjusted': False}
    assert pose['x'] >= .22
    assert all(yaw == 0 for _, _, yaw in commands)
