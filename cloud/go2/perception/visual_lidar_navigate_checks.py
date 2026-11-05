"""Offline checks for the RGB/LiDAR to A* navigation handoff."""
from types import SimpleNamespace

import numpy as np
import pytest

from cloud.go2.perception import visual_lidar_navigate as fusion
from cloud.go2.perception.visual_target import TargetUnlocalized


@pytest.mark.asyncio
async def test_locate_uses_closest_available_voxel_without_time_gate(monkeypatch):
    frame = {"frame_id": 7, "received_at": 10.0,
             "odom_session_id": "s", "pose_at_capture": {"session_id": "s"}}
    before = {"data": {"frame_id": "odom", "source": "before"}}
    after = {"data": {"frame_id": "odom", "source": "after"}}

    class Robot:
        is_connected = True
        _odom_session_id = "s"
        reads = 0

        def latest_frame_snapshot(self):
            return b"jpeg", frame

        @property
        def voxel_raw(self):
            return before if self.reads == 0 else after

        @property
        def voxel_received_at(self):
            value = 9.3 if self.reads == 0 else 15.0
            self.reads += 1
            return value

    robot = Robot()
    selected = []
    async def detect(*_):
        return {"found": True, "bbox_1000": [500, 400, 600, 600]}

    monkeypatch.setattr(fusion, "go2", robot)
    monkeypatch.setattr(fusion, "navigator", SimpleNamespace(
        _snapshot=lambda: (({"session_id": "s"}, object()), None)))
    monkeypatch.setattr(fusion, "_save_diagnostic_frame", lambda *_: None)
    monkeypatch.setattr(fusion, "detect_visible_target", detect)
    monkeypatch.setattr(fusion, "target_offset", lambda *_: (0.0, 0.2))
    monkeypatch.setattr(fusion, "_paired_target_xy", lambda _frame, voxel, at, _bbox: (
        selected.append((voxel, at)) or (2.0, -0.5), object(), object()))
    monkeypatch.setattr(fusion, "save_paired_sample", lambda *_: None)
    monkeypatch.setattr(fusion, "emit_agent_event", lambda *a, **k: None)
    result = await fusion._locate("毛绒玩具")
    assert result[0] == (2.0, -0.5)
    assert selected == [(before, 9.3)]


def test_paired_visual_box_locates_object_not_floor(monkeypatch):
    monkeypatch.setattr(fusion, "emit_agent_event", lambda *a, **k: None)
    pose = {"x": 0.0, "y": 0.0, "z": 0.3, "heading": 0.0,
            "orientation": [0, 0, 0, 1], "session_id": "s"}
    floor = [[x, y, -0.05] for x in np.arange(0.3, 1.5, 0.1)
             for y in np.arange(-0.6, 0.61, 0.1)]
    object_points = [[2.0, -0.5, 0.2], [2.0, -0.55, 0.25],
                     [2.05, -0.5, 0.2]]
    voxel = {"data": {"frame_id": "odom", "origin": [-3, -3, -1],
                      "width": [120, 120, 40], "resolution": 0.05,
                      "data": {"points": floor + object_points}}}
    frame = {"frame_id": 1, "width": 640, "height": 360,
             "received_at": 10.0, "odom_session_id": "s", "pose_at_capture": pose}
    target, grid, _ = fusion._paired_target_xy(
        frame, voxel, 10.04, [600, 500, 700, 650])
    assert grid.measured_floor_z is not None
    assert np.allclose(target, [2.0, -0.5], atol=0.06)
    target_later, _, _ = fusion._paired_target_xy(
        frame, voxel, 9.0, [600, 500, 700, 650])
    assert np.allclose(target_later, target)


@pytest.mark.asyncio
async def test_visible_target_hands_reachable_stop_to_navigator_and_rechecks(monkeypatch):
    calls = []
    pose = {"x": 0.0, "y": 0.0, "heading": 0.0, "session_id": "s"}
    final_pose = {"x": 1.8, "y": -0.8, "heading": 0.0, "session_id": "s"}
    observations = iter([((2.28, -0.85), pose, object(), 0.3),
                         ((2.28, -0.85), final_pose, object(), 0.05)])

    async def locate(_target):
        calls.append("observe")
        return next(observations)

    async def navigate(x, y, **kwargs):
        calls.append(("navigate", x, y, kwargs))
        return {"status": "succeeded", "task_id": "n1"}

    monkeypatch.setattr(fusion, "go2", SimpleNamespace(is_connected=True, fsm_state="standing"))
    monkeypatch.setattr(fusion, "current_context", lambda: SimpleNamespace(session_id="user"))
    monkeypatch.setattr(fusion, "control_lease", SimpleNamespace(acquire=lambda _: None))
    monkeypatch.setattr(fusion, "_locate", locate)
    monkeypatch.setattr(fusion, "select_reachable_stop", lambda *_: {
        "x": 1.8, "y": -0.8, "heading": 0.0,
        "distance_to_target_m": 0.48, "already_near": False})
    monkeypatch.setattr(fusion, "navigator", SimpleNamespace(go_to_coordinate=navigate))
    monkeypatch.setattr(fusion, "emit_agent_event", lambda *a, **k: None)

    result = await fusion.navigate_to_visible_target("毛绒玩具")
    assert result["result"] == "ok"
    assert result["confirmation"] == "odom_stopped_and_rgb_lidar_reobserved"
    assert calls[0] == "observe" and calls[-1] == "observe"
    _, x, y, kwargs = calls[1]
    assert (x, y) == (1.8, -0.8)
    assert kwargs["owner_session_id"] == "user"
    assert kwargs["keepout"] == (2.28, -0.85, fusion.TARGET_CLEARANCE_M)
    assert kwargs["speed_limit"] == fusion.STOP_SPEED_M_S
    assert kwargs["holonomic"] is True
    assert kwargs["face_target"] == (2.28, -0.85)
    assert callable(kwargs["face_target_bias_rad"])
    assert kwargs["face_target_bias_rad"]() == pytest.approx(
        fusion._visual_heading_bias((2.28, -0.85), pose, 0.3))
    assert kwargs["stop_when_near_target_m"] == fusion.APPROACH_STOP_RADIUS_M
    assert kwargs["stop_cell_tolerance_m"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("navigation_status", ["succeeded", "blocked"])
async def test_game_arrival_uses_navigation_receipt_without_visual_recheck(
        monkeypatch, navigation_status):
    observations = []

    async def locate(_target):
        observations.append(_target)
        if len(observations) > 1:
            raise AssertionError("arrival must not trigger another visual detection")
        return ((2.0, 0.0), {"x": 0.0, "y": 0.0, "heading": 0.0,
                              "session_id": "s"}, object(), 0.0)

    navigation_options = []

    async def navigate(*_args, **kwargs):
        navigation_options.append(kwargs)
        return {"status": navigation_status, "reason": "no_known_free_path"}

    monkeypatch.setattr(fusion, "go2", SimpleNamespace(is_connected=True, fsm_state="standing"))
    monkeypatch.setattr(fusion, "current_context", lambda: SimpleNamespace(session_id="game"))
    monkeypatch.setattr(fusion, "control_lease", SimpleNamespace(acquire=lambda _: None))
    monkeypatch.setattr(fusion, "_locate", locate)
    monkeypatch.setattr(fusion, "select_reachable_stop", lambda *_: {
        "x": 1.5, "y": 0.0, "already_near": False})
    monkeypatch.setattr(fusion, "navigator", SimpleNamespace(go_to_coordinate=navigate))
    monkeypatch.setattr(fusion, "emit_agent_event", lambda *a, **k: None)

    with fusion.accept_navigation_arrival():
        result = await fusion.navigate_to_visible_target("白色猫头鹰")

    assert observations == ["白色猫头鹰"]
    assert navigation_options[0]["stop_cell_tolerance_m"] == fusion.GAME_STOP_CELL_TOLERANCE_M
    assert result["result"] == ("ok" if navigation_status == "succeeded" else "blocked")
    if navigation_status == "succeeded":
        assert result["confirmation"] == "odom_stopped_near_mapped_stop"
    assert fusion._arrival_by_navigation.get() is False


@pytest.mark.asyncio
async def test_game_already_near_requires_confirmed_stop(monkeypatch):
    async def locate(_target):
        return ((0.7, 0.0), {"x": 0.0, "y": 0.0, "heading": 0.0,
                             "session_id": "s"}, object(), 0.0)

    async def confirm(*_args):
        return False

    monkeypatch.setattr(fusion, "go2", SimpleNamespace(is_connected=True, fsm_state="standing"))
    monkeypatch.setattr(fusion, "current_context", lambda: SimpleNamespace(session_id="game"))
    monkeypatch.setattr(fusion, "control_lease", SimpleNamespace(acquire=lambda _: None))
    monkeypatch.setattr(fusion, "_locate", locate)
    monkeypatch.setattr(fusion, "select_reachable_stop", lambda *_: {
        "x": 0.0, "y": 0.0, "already_near": True})
    monkeypatch.setattr(fusion, "navigator", SimpleNamespace(_confirm_stopped=confirm))
    monkeypatch.setattr(fusion, "emit_agent_event", lambda *a, **k: None)

    with fusion.accept_navigation_arrival():
        result = await fusion.navigate_to_visible_target("白色猫头鹰")

    assert result["result"] == "blocked"
    assert result["reason"] == "arrival_unconfirmed"


def test_visual_heading_bias_corrects_map_camera_disagreement():
    pose = {"x": 0.0, "y": 0.0, "heading": 0.0}
    assert fusion._visual_heading_bias((2.0, 0.0), pose, 0.38) == pytest.approx(
        -np.arctan(0.38))


@pytest.mark.asyncio
async def test_unlocalized_target_never_starts_navigation(monkeypatch):
    calls = []

    async def locate(_target):
        raise TargetUnlocalized("object_returns_unavailable")

    async def navigate(*a, **k):
        calls.append((a, k))

    monkeypatch.setattr(fusion, "go2", SimpleNamespace(is_connected=True, fsm_state="standing"))
    monkeypatch.setattr(fusion, "current_context", lambda: SimpleNamespace(session_id="user"))
    monkeypatch.setattr(fusion, "control_lease", SimpleNamespace(acquire=lambda _: None))
    monkeypatch.setattr(fusion, "_locate", locate)
    monkeypatch.setattr(fusion, "navigator", SimpleNamespace(go_to_coordinate=navigate))
    result = await fusion.navigate_to_visible_target("毛绒玩具")
    assert result["result"] == "blocked"
    assert result["reason"] == "object_returns_unavailable"
    assert calls == []


@pytest.mark.asyncio
async def test_public_visual_navigation_tool_uses_rgb_lidar_pipeline(monkeypatch):
    from cloud.go2.tools import tools as go2_tools

    async def run(target):
        return {"result": "ok", "target": target, "confirmation": "rgb_lidar"}

    monkeypatch.setattr(fusion, "navigate_to_visible_target", run)
    result = await go2_tools.TOOL_FN_MAP["go2_navigate_to_visible_target"]("毛绒玩具")
    assert result == {"result": "ok", "target": "毛绒玩具", "confirmation": "rgb_lidar"}


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
