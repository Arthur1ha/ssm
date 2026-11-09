"""Offline checks for map-gated visual approach; no physical commands are sent."""
import math
import os
import stat
import time
from types import SimpleNamespace

import numpy as np
import pytest

from cloud.go2.perception import visual_target as approach
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
    approach.save_diagnostic_frame(b"\xff\xd8jpeg\xff\xd9", frame)
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


def test_turn_clearance_reports_floor_coverage_and_blocks_measured_obstacle():
    grid = OccupancyGrid.__new__(OccupancyGrid)
    grid.origin = [-1.0, -1.0]
    grid.width = [40, 40]
    grid.resolution = 0.05
    grid.cells = np.full((40, 40), FREE, dtype=np.uint8)
    grid.known_floor = np.ones((40, 40), dtype=bool)
    grid.obstacles = np.zeros((40, 40), dtype=bool)
    assert grid.turn_clearance_details(0.0, 0.0)["clear"]
    grid.known_floor[20, 24] = False
    assert grid.turn_clearance_details(0.0, 0.0)["clear"]
    grid.obstacles[20, 26] = True
    details = grid.turn_clearance_details(0.0, 0.0)
    assert not details["clear"] and not grid.is_turn_clear(0.0, 0.0)
    assert details["blockers"] == ["measured_obstacle"]
    assert details["unknown_floor_cells"] == 1
    assert details["obstacle_cells"] == 1
    assert details["nearest_obstacle_m"] == 0.3


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




@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['same_frame', 'different_session', 'moved_target'])
async def test_unique_executor_rejects_inconsistent_confirmation_before_motion(monkeypatch, mismatch):
    from cloud.go2.navigation import target_approach as executor
    from cloud.go2.control import ControlLease
    from cloud.space.execution import ExecutionContext, use_context

    context = ExecutionContext('confirm-test', 'visitor', False, expires_at=int(time.time() + 60))
    monkeypatch.setattr(executor, 'control_lease', ControlLease())
    monkeypatch.setattr(executor, 'go2', SimpleNamespace(is_connected=True, body_state='standing'))
    monkeypatch.setattr(executor.visual_target, 'load_calibration', lambda **_: {})
    calls = []
    async def locate(*args, **kwargs):
        calls.append(kwargs)
        second = len(calls) == 2
        return {'odom_session_id': 'other' if second and mismatch == 'different_session' else 's',
                'source_frame_id': 1 if mismatch == 'same_frame' else len(calls),
                'source_received_at': len(calls),
                'x': 4. if second and mismatch == 'moved_target' else 2., 'y': 0.}, {}, None
    monkeypatch.setattr(executor.visual_target, 'locate_ground_target', locate)
    monkeypatch.setattr(executor, 'navigator', SimpleNamespace(
        snapshot=lambda: pytest.fail('inconsistent candidate must not reach navigation')))
    with use_context(context):
        result = await executor.navigate_to_visible_target('owl')
    assert result['result'] == 'blocked'
    assert result['reason'] == 'target_confirmation_mismatch'
    assert calls[1]['after_frame_id'] == 1


@pytest.mark.asyncio
async def test_unique_executor_success_requires_fresh_post_navigation_recheck(monkeypatch):
    from cloud.go2.navigation import target_approach as executor
    from cloud.go2.control import ControlLease
    from cloud.space.execution import ExecutionContext, use_context

    context = ExecutionContext('arrival-test', 'visitor', False, expires_at=int(time.time() + 60))
    monkeypatch.setattr(executor, 'control_lease', ControlLease())
    monkeypatch.setattr(executor, 'go2', SimpleNamespace(is_connected=True, body_state='standing'))
    monkeypatch.setattr(executor.visual_target, 'load_calibration', lambda **_: {})
    calls, moved = [], []
    start = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 's'}
    finish = {**start, 'x': 1.}
    async def locate(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 3:
            assert moved and kwargs['after_received_at'] >= moved[0]
        return {'odom_session_id': 's', 'source_frame_id': len(calls),
                'source_received_at': time.monotonic(), 'x': 2.55, 'y': 0.,
                'uncertainty_m': .05}, finish if moved else start, None
    async def navigate(x, y, **kwargs):
        moved.append(time.monotonic())
        assert kwargs['owner_session_id'] == context.session_id
        return {'status': 'succeeded', 'last_trusted_pose': finish}
    monkeypatch.setattr(executor.visual_target, 'locate_ground_target', locate)
    monkeypatch.setattr(executor.visual_target, 'select_standoff',
                        lambda *_: {'x': 1., 'y': 0., 'heading': 0.})
    monkeypatch.setattr(executor, 'navigator', SimpleNamespace(
        snapshot=lambda: ((start, object()), None), go_to_coordinate=navigate))
    with use_context(context):
        result = await executor.navigate_to_visible_target('owl')
    assert result['result'] == 'ok'
    assert result['confirmation'] == 'odom_stopped_and_visual_recheck'
    assert len(calls) == 3
    assert result['front_clearance_to_contact_m'] == 1.2


@pytest.mark.asyncio
async def test_authorization_expiry_cancels_pending_visual_inference(monkeypatch):
    import asyncio
    from cloud.go2.navigation import target_approach as executor
    from cloud.go2.control import ControlLease
    from cloud.space.execution import ExecutionContext

    context = ExecutionContext('expiry-test', 'visitor', False, expires_at=time.time() + .05)
    lease = ControlLease()
    lease.acquire(context)
    monkeypatch.setattr(executor, 'control_lease', lease)
    cancelled = asyncio.Event()
    async def inference():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    with pytest.raises(TargetUnlocalized, match='authorization_expired'):
        await executor._await_authorized(inference(), context)
    assert cancelled.is_set()
