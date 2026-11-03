"""Offline checks for map-gated visual approach; no physical commands are sent."""
import math
import os
import stat
import time
from types import SimpleNamespace

import numpy as np
import pytest

from cloud.go2.perception import visual_approach as approach
from cloud.go2.perception.visual_target import TargetUnlocalized
from cloud.go2.navigation.occupancy_grid import OccupancyGrid, FREE


def test_visual_offset_uses_any_matching_visible_target():
    frame = {"width": 640, "height": 480}
    observation = {"found": True, "count": 2, "static": False, "confidence": 0.1,
                   "bbox_1000": [400, 100, 600, 500]}
    offset, height = approach.target_offset(observation, frame)
    assert offset == 0
    assert height == 0.4
    with pytest.raises(TargetUnlocalized, match="target_not_visible"):
        approach.target_offset({**observation, "found": False}, frame)
    assert approach.target_offset({**observation, "bbox_1000": [0, 100, 1000, 1000]}, frame) == (0, 0.9)


def test_missing_target_records_visibility_decision(monkeypatch):
    events = []
    monkeypatch.setattr(approach, "emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))
    observation = {"found": False, "bbox_1000": None}
    with pytest.raises(TargetUnlocalized, match="target_not_visible"):
        approach.target_offset(observation, {"width": 640, "height": 480, "frame_id": 42})
    event, fields = events[-1]
    assert event == "vision.gate" and fields["gate"] == "target_visible"
    assert fields["frame_id"] == 42
    assert fields["found"] is False and fields["reason"] == "target_not_visible"


def test_low_confidence_and_static_metadata_do_not_block_visible_target(monkeypatch):
    events = []
    monkeypatch.setattr(approach, "emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))
    observation = {"found": True, "count": 5, "static": False, "confidence": 0.0,
                   "bbox_1000": [606, 567, 652, 662]}
    offset, height = approach.target_offset(observation, {"width": 640, "height": 360})
    assert math.isclose(offset, 0.258) and math.isclose(height, 0.095)
    assert events[-1][1]["gate"] == "target_geometry"
    assert events[-1][1]["bbox_pixels"] == [387.8, 204.1, 417.3, 238.3]


def test_diagnostic_frame_is_private_and_linked_to_run(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(approach, "CAPTURE_DIR", tmp_path / "vision")
    monkeypatch.setattr(approach, "current_event_context", lambda: {"run_id": "go2-run"})
    monkeypatch.setattr(approach, "emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))
    frame = {"frame_id": 42}
    approach._save_diagnostic_frame(b"\xff\xd8jpeg\xff\xd9", frame)
    event, fields = events[-1]
    assert event == "vision.observe" and fields["saved"] is True
    path = tmp_path / "vision" / fields["artifact"].split("/")[-1]
    assert path.read_bytes() == b"\xff\xd8jpeg\xff\xd9"
    assert path.name.startswith("go2-run-42-")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_old_diagnostic_frame_expires_with_session_logs(tmp_path):
    from cloud.observability.logging_config import _cleanup_old_sessions
    folder = tmp_path / "vision"
    folder.mkdir()
    old = folder / "old.jpg"
    old_points = folder / "old.npz"
    recent = folder / "recent.jpg"
    old.write_bytes(b"old")
    old_points.write_bytes(b"old points")
    recent.write_bytes(b"recent")
    stale_at = time.time() - 15 * 86400
    os.utime(old, (stale_at, stale_at))
    os.utime(old_points, (stale_at, stale_at))
    _cleanup_old_sessions(tmp_path)
    assert not old.exists() and not old_points.exists() and recent.exists()


def test_invalid_bbox_logs_why_it_was_rejected(caplog):
    frame = {"width": 640, "height": 480}
    observation = {"found": True,
                   "bbox_1000": [10, 20, 1001, 300]}
    with pytest.raises(TargetUnlocalized, match="vision_result_invalid"):
        approach.target_offset(observation, frame)
    assert "bbox outside normalized 1000 range" in caplog.text
    assert "640x480" in caplog.text


@pytest.mark.asyncio
async def test_observe_accepts_visible_target_without_old_identity_fields(monkeypatch):
    now = time.monotonic()
    pose = {"x": 0.0, "y": 0.0, "heading": 0.0, "session_id": "odom-session"}
    grid = SimpleNamespace(frame_id="odom", resolution=0.05,
                           has_free_space_evidence=True)
    frame = {"frame_id": 43, "width": 640, "height": 360,
             "received_at": now, "odom_session_id": "odom-session"}

    async def detect(jpeg, target, captured_frame):
        assert jpeg == b"jpeg" and target == "白色毛绒玩具"
        assert captured_frame is frame
        return {"found": True, "bbox_1000": [606, 567, 652, 662],
                "count": 5, "static": False, "confidence": 0.0}

    monkeypatch.setattr(approach.navigator, "_snapshot", lambda: ((pose.copy(), grid), None))
    monkeypatch.setattr(approach, "go2", SimpleNamespace(
        latest_frame_snapshot=lambda: (b"jpeg", frame),
        _odom_session_id="odom-session", odom_received_at=now,
        voxel_received_at=now))
    monkeypatch.setattr(approach, "_save_diagnostic_frame", lambda *_: None)
    monkeypatch.setattr(approach, "save_paired_sample", lambda *_: None)
    monkeypatch.setattr(approach, "detect_visible_target", detect)
    monkeypatch.setattr(approach, "emit_agent_event", lambda *_a, **_k: None)
    offset, height, _ = await approach._observe("白色毛绒玩具")
    assert math.isclose(offset, 0.258) and math.isclose(height, 0.095)


@pytest.mark.asyncio
async def test_pose_change_recaptures_before_using_visual_direction(monkeypatch):
    frames = iter([TargetUnlocalized("pose_changed_during_detection"),
                   (0.08, 0.23, {"x": 2.07, "y": -0.18, "heading": -0.21})])
    events = []
    calls = []

    async def observe_once(target):
        calls.append(target)
        result = next(frames)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(approach, "_observe_once", observe_once)
    monkeypatch.setattr(approach, "emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))
    assert await approach._observe("毛绒玩具") == (
        0.08, 0.23, {"x": 2.07, "y": -0.18, "heading": -0.21})
    assert calls == ["毛绒玩具", "毛绒玩具"]
    assert events == [("vision.observe", {
        "phase": "recapture_after_pose_change",
        "reason": "pose_changed_during_detection"})]


def test_forward_waypoint_requires_known_free_map(monkeypatch):
    pose = {"x": 0.0, "y": 0.0, "heading": 0.0}
    grid = SimpleNamespace(is_body_clear=lambda *_: True)
    monkeypatch.setattr(approach, "_raycast", lambda *_: 0.4)
    with pytest.raises(TargetUnlocalized, match="no_known_free_forward_path"):
        approach.forward_waypoint(grid, pose)
    monkeypatch.setattr(approach, "_raycast", lambda *_: 1.0)
    assert approach.forward_waypoint(grid, pose) == (approach.STEP_M, 0.0)


def test_turn_clearance_reports_unknown_floor_and_obstacle_separately():
    grid = OccupancyGrid.__new__(OccupancyGrid)
    grid.origin = [-1.0, -1.0]
    grid.width = [40, 40]
    grid.resolution = 0.05
    grid.cells = np.full((40, 40), FREE, dtype=np.uint8)
    grid.known_floor = np.ones((40, 40), dtype=bool)
    grid.obstacles = np.zeros((40, 40), dtype=bool)
    assert grid.turn_clearance_details(0.0, 0.0)["clear"]
    grid.known_floor[20, 24] = False
    grid.obstacles[20, 26] = True
    details = grid.turn_clearance_details(0.0, 0.0)
    assert not details["clear"] and not grid.is_turn_clear(0.0, 0.0)
    assert details["blockers"] == ["unmeasured_floor", "measured_obstacle"]
    assert details["unknown_floor_cells"] == 1
    assert details["obstacle_cells"] == 1
    assert details["nearest_obstacle_m"] == 0.3


@pytest.mark.asyncio
async def test_approach_reobserves_and_reports_motion_without_arrival(monkeypatch):
    pose = {"x": 0.0, "y": 0.0, "heading": 0.0, "session_id": "s"}
    grid = SimpleNamespace(is_turn_clear=lambda *_: True)
    observations = iter([(0.0, 0.2, pose.copy()),
                         (0.0, 0.5, {**pose, "x": approach.STEP_M})])
    calls = []
    events = []

    async def observe(_target):
        return next(observations)

    async def navigate(x, y, *, owner_session_id, heading=None, speed_limit=None):
        calls.append((x, y, heading, speed_limit))
        pose["x"] = x
        return {"status": "succeeded", "last_trusted_pose": pose.copy()}

    monkeypatch.setattr(approach.go2, "is_connected", True)
    monkeypatch.setattr(approach.go2, "fsm_state", "standing")
    monkeypatch.setattr(approach.go2, "_sport_state_observed_at", time.monotonic(), raising=False)
    monkeypatch.setattr(approach, "_observe", observe)
    monkeypatch.setattr(approach.navigator, "_snapshot", lambda: ((pose.copy(), grid), None))
    monkeypatch.setattr(approach.navigator, "go_to_coordinate", navigate)
    monkeypatch.setattr(approach, "forward_waypoint", lambda _grid, p: (p["x"] + approach.STEP_M, p["y"]))
    monkeypatch.setattr(approach, "current_context", lambda: SimpleNamespace(session_id="user"))
    monkeypatch.setattr(approach.control_lease, "acquire", lambda _context: None)
    monkeypatch.setattr(approach, "emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))

    result = await approach.approach_visible_target("白色猫头鹰")
    assert result["result"] == "ok"
    assert math.isclose(result["approached_m"], approach.STEP_M)
    assert "尚未确认到达" in result["message"]
    assert calls == [(approach.STEP_M, 0.0, None, 0.15)]
    assert [(fields["phase"], fields["kind"]) for event, fields in events
            if event == "vision.motion"] == [("request", "forward"), ("result", "forward")]


@pytest.mark.asyncio
async def test_approach_blocks_before_motion_without_map(monkeypatch):
    async def no_map(_target):
        raise TargetUnlocalized("map_unavailable")

    async def no_navigation(*_args, **_kwargs):
        raise AssertionError("motion must not start without a map")

    async def no_switch_query():
        raise AssertionError("built-in avoidance switch is not a prerequisite")

    monkeypatch.setattr(approach.go2, "is_connected", True)
    monkeypatch.setattr(approach.go2, "fsm_state", "standing")
    monkeypatch.setattr(approach.go2, "_sport_state_observed_at", time.monotonic(), raising=False)
    monkeypatch.setattr(approach.go2, "read_avoidance_switch", no_switch_query)
    monkeypatch.setattr(approach, "_observe", no_map)
    monkeypatch.setattr(approach.navigator, "go_to_coordinate", no_navigation)
    monkeypatch.setattr(approach, "current_context", lambda: SimpleNamespace(session_id="user"))
    monkeypatch.setattr(approach.control_lease, "acquire", lambda _context: None)
    result = await approach.approach_visible_target("白色猫头鹰")
    assert result["result"] == "blocked"
    assert result["reason"] == "map_unavailable"
    assert result["approached_m"] == 0


@pytest.mark.asyncio
async def test_wrong_direction_after_turn_stops_before_forward_motion(monkeypatch):
    pose = {"x": 0.0, "y": 0.0, "heading": 0.0, "session_id": "s"}
    grid = SimpleNamespace(is_turn_clear=lambda *_: True)
    observations = iter([(-0.5, 0.2, pose), (-0.6, 0.2, pose)])
    calls = []

    async def observe(_target):
        return next(observations)

    async def navigate(x, y, *, owner_session_id, heading=None, speed_limit=None):
        calls.append((x, y, heading))
        return {"status": "succeeded"}

    monkeypatch.setattr(approach.go2, "is_connected", True)
    monkeypatch.setattr(approach.go2, "fsm_state", "standing")
    monkeypatch.setattr(approach.go2, "_sport_state_observed_at", time.monotonic(), raising=False)
    monkeypatch.setattr(approach, "_observe", observe)
    monkeypatch.setattr(approach.navigator, "_snapshot", lambda: ((pose, grid), None))
    monkeypatch.setattr(approach.navigator, "go_to_coordinate", navigate)
    monkeypatch.setattr(approach, "current_context", lambda: SimpleNamespace(session_id="user"))
    monkeypatch.setattr(approach.control_lease, "acquire", lambda _context: None)
    result = await approach.approach_visible_target("白色猫头鹰")
    assert result["reason"] == "visual_turn_not_confirmed"
    assert result["approached_m"] == 0
    assert len(calls) == 1 and calls[0][2] > 0


@pytest.mark.asyncio
async def test_front_target_uses_mapped_forward_path_when_turn_space_is_unknown(monkeypatch):
    pose = {"x": 0.0, "y": 0.0, "heading": 0.0, "session_id": "s"}
    grid = SimpleNamespace(turn_clearance_details=lambda *_: {
        "clear": False, "blockers": ["unmeasured_floor"],
        "unknown_floor_cells": 3})
    observations = iter([(0.344, 0.1, pose.copy()),
                         (0.2, 0.5, {**pose, "x": approach.STEP_M})])
    calls = []
    events = []

    async def observe(_target):
        return next(observations)

    async def navigate(x, y, *, owner_session_id, heading=None, speed_limit=None):
        calls.append((x, y, heading))
        pose["x"] = x
        return {"status": "succeeded", "last_trusted_pose": pose.copy()}

    monkeypatch.setattr(approach.go2, "is_connected", True)
    monkeypatch.setattr(approach.go2, "fsm_state", "standing")
    monkeypatch.setattr(approach.go2, "_sport_state_observed_at", time.monotonic(), raising=False)
    monkeypatch.setattr(approach, "_observe", observe)
    monkeypatch.setattr(approach.navigator, "_snapshot", lambda: ((pose.copy(), grid), None))
    monkeypatch.setattr(approach.navigator, "go_to_coordinate", navigate)
    monkeypatch.setattr(approach, "forward_waypoint", lambda _grid, p: (p["x"] + approach.STEP_M, p["y"]))
    monkeypatch.setattr(approach, "current_context", lambda: SimpleNamespace(session_id="user"))
    monkeypatch.setattr(approach.control_lease, "acquire", lambda _context: None)
    monkeypatch.setattr(approach, "emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))

    result = await approach.approach_visible_target("白色毛绒玩具")
    assert result["result"] == "ok"
    assert calls == [(approach.STEP_M, 0.0, None)]
    assert any(fields.get("gate") == "turn_clearance"
               and fields["map_evidence"]["blockers"] == ["unmeasured_floor"]
               for event, fields in events if event == "vision.gate")


@pytest.mark.asyncio
async def test_failed_navigation_reports_measured_partial_advance(monkeypatch):
    pose = {"x": 0.0, "y": 0.0, "heading": 0.0, "session_id": "s"}
    grid = SimpleNamespace()

    async def observe(_target):
        return 0.0, 0.1, pose

    async def navigate(*_args, **_kwargs):
        return {"status": "blocked", "reason": "body_clearance_unavailable",
                "message": "当前机身朝向与障碍距离不足，已停止",
                "last_trusted_pose": {**pose, "x": 0.13}}

    monkeypatch.setattr(approach.go2, "is_connected", True)
    monkeypatch.setattr(approach.go2, "fsm_state", "standing")
    monkeypatch.setattr(approach.go2, "_sport_state_observed_at", time.monotonic(), raising=False)
    monkeypatch.setattr(approach, "_observe", observe)
    monkeypatch.setattr(approach.navigator, "_snapshot", lambda: ((pose, grid), None))
    monkeypatch.setattr(approach.navigator, "go_to_coordinate", navigate)
    monkeypatch.setattr(approach, "forward_waypoint", lambda *_: (approach.STEP_M, 0.0))
    monkeypatch.setattr(approach, "current_context", lambda: SimpleNamespace(session_id="user"))
    monkeypatch.setattr(approach.control_lease, "acquire", lambda _context: None)
    monkeypatch.setattr(approach, "emit_agent_event", lambda *_a, **_k: None)

    result = await approach.approach_visible_target("白色毛绒玩具")
    assert result["approached_m"] == 0.13
    assert "0.13 米" in result["message"]
    assert "已停止 停止" not in result["message"]


@pytest.mark.asyncio
async def test_side_target_does_not_advance_without_turn_clearance(monkeypatch):
    pose = {"x": 0.0, "y": 0.0, "heading": 0.0, "session_id": "s"}
    grid = SimpleNamespace(is_turn_clear=lambda *_: False)

    async def no_navigation(*_args, **_kwargs):
        raise AssertionError("side target must not cause forward movement")

    async def observe(_target):
        return 0.7, 0.1, pose

    monkeypatch.setattr(approach.go2, "is_connected", True)
    monkeypatch.setattr(approach.go2, "fsm_state", "standing")
    monkeypatch.setattr(approach.go2, "_sport_state_observed_at", time.monotonic(), raising=False)
    monkeypatch.setattr(approach, "_observe", observe)
    monkeypatch.setattr(approach.navigator, "_snapshot", lambda: ((pose, grid), None))
    monkeypatch.setattr(approach.navigator, "go_to_coordinate", no_navigation)
    monkeypatch.setattr(approach, "current_context", lambda: SimpleNamespace(session_id="user"))
    monkeypatch.setattr(approach.control_lease, "acquire", lambda _context: None)
    monkeypatch.setattr(approach, "emit_agent_event", lambda *_a, **_k: None)

    result = await approach.approach_visible_target("白色毛绒玩具")
    assert result["reason"] == "turn_clearance_unavailable"
    assert result["approached_m"] == 0


@pytest.mark.asyncio
async def test_visual_approach_speed_limit_reaches_navigator_velocity(monkeypatch):
    from cloud.go2.navigation import navigator as nav_module

    commands = []
    cells = np.zeros((50, 50), dtype=bool)
    grid = SimpleNamespace(
        frame_id="odom", has_free_space_evidence=True, resolution=0.05, grid=cells,
        odom_to_grid=lambda x, y: (int((x + 1.0) / 0.05), int((y + 1.0) / 0.05)),
        grid_to_odom=lambda x, y: (-1.0 + (x + 0.5) * 0.05, -1.0 + (y + 0.5) * 0.05),
        is_free=lambda x, y: 0 <= x < 50 and 0 <= y < 50,
        is_body_clear=lambda *_: True,
    )
    now = time.monotonic()
    robot = SimpleNamespace(
        is_connected=True, odom={"x": 0.0, "y": 0.0, "heading": 0.0},
        odom_received_at=now, voxel_received_at=now,
        odom_frame_id="odom", voxel_frame_id="odom",
        _odom_session_id="session", occupancy_grid=grid,
        move_velocity=lambda x, y, yaw, **kwargs: commands.append((x, y, yaw)),
    )
    monkeypatch.setattr(nav_module, "go2", robot)
    monkeypatch.setattr(nav_module, "NAV_TIMEOUT", 0.3)
    result = await nav_module.Navigator().go_to_coordinate(0.8, 0.0, speed_limit=0.15)
    assert result["reason"] == "navigation_timeout"
    assert any(0 < x <= 0.15 for x, _, _ in commands)
    assert all(x <= 0.15 for x, _, _ in commands)


@pytest.mark.asyncio
async def test_heading_only_turn_rejects_unmapped_body_clearance(monkeypatch):
    from cloud.go2.navigation import navigator as nav_module

    commands = []
    pose = {"x": 0.0, "y": 0.0, "heading": 0.0, "session_id": "session"}
    grid = SimpleNamespace(is_turn_clear=lambda *_: False)
    robot = SimpleNamespace(_odom_session_id="session",
                            move_velocity=lambda *args, **kwargs: commands.append(args))
    monkeypatch.setattr(nav_module, "go2", robot)
    nav = nav_module.Navigator()
    monkeypatch.setattr(nav, "_snapshot", lambda: ((pose, grid), None))
    monkeypatch.setattr(nav, "_plan_path", lambda *_: ([(0, 0)], grid, (0.0, 0.0, False)))
    result = await nav.go_to_coordinate(0.0, 0.0, heading=0.35)
    assert result["status"] == "blocked"
    assert result["reason"] == "turn_clearance_unavailable"
    assert commands and all(command == (0, 0, 0) for command in commands)
