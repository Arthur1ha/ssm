"""Bounded visual servoing over the robot's measured local free-space map.

This only confirms an approach toward a visible target. It does not estimate
the target's world position or claim arrival at it.
"""
from __future__ import annotations

import math
import logging
import hashlib
import os
import re
import time
import uuid

from cloud.go2.connection import go2
from cloud.go2.control import control_lease
from cloud.go2.execution import current_context
from cloud.go2.navigation.frontier import _raycast
from cloud.go2.navigation.navigator import navigator
from cloud.go2.perception.visual_target import TargetUnlocalized, detect_visible_target
from cloud.go2.perception.rgb_lidar import save_paired_sample
from cloud.observability import current_event_context, emit_agent_event
from cloud.observability.logging_config import LOG_DIR

logger = logging.getLogger(__name__)
CAPTURE_DIR = LOG_DIR / "vision"
MAX_CAPTURE_BYTES = 2_000_000

MAX_FRAME_AGE_S = 0.8
MAX_STEPS = 4
MAX_TURNS = 4
STEP_M = 0.55
TURN_RAD = 0.35
CENTER_TOLERANCE = 0.15
MIN_FORWARD_FREE_M = 0.85

_REASONS = {
    "frame_unavailable": "没有可用的相机画面",
    "frame_stale": "相机画面过期或不属于当前定位会话",
    "target_not_visible": "画面中没有找到所说的物体",
    "vision_result_invalid": "视觉识别没有给出有效的目标区域",
    "localization_unavailable": "当前位置数据不可用",
    "map_unavailable": "当前地图没有可用的已知自由区域",
    "frame_mismatch": "地图和定位坐标不一致",
    "odom_session_mismatch": "定位会话在观察时发生变化",
    "pose_changed_during_detection": "观察期间机身发生移动",
    "no_known_free_forward_path": "目标方向没有足够的已知可通行区域",
    "body_clearance_unavailable": "机身前方空间不足",
    "turn_clearance_unavailable": "地图未确认周围有足够的原地转向空间",
    "visual_turn_not_confirmed": "转向后目标没有朝画面中央移动，方向未确认",
    "movement_unconfirmed": "里程计未能确认短距离前进",
}


def _save_diagnostic_frame(jpeg: bytes, frame: dict):
    """Save only frames used for a visual approach, outside the public app tree."""
    if not jpeg or len(jpeg) > MAX_CAPTURE_BYTES:
        emit_agent_event("vision.observe", phase="frame_capture", saved=False,
                         frame_id=frame.get("frame_id"), size_bytes=len(jpeg),
                         reason="capture_size_invalid")
        return None
    context = current_event_context()
    run_id = context.get("run_id") or uuid.uuid4().hex
    safe_run = re.sub(r"[^A-Za-z0-9_-]", "_", str(run_id))[:80]
    frame_id = frame.get("frame_id")
    safe_frame = str(frame_id) if type(frame_id) is int and frame_id >= 0 else "unknown"
    digest = hashlib.sha256(jpeg).hexdigest()
    path = CAPTURE_DIR / f"{safe_run}-{safe_frame}-{digest[:12]}.jpg"
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(jpeg)
            while view:
                written = os.write(fd, view)
                if written == 0:
                    raise OSError("frame capture write made no progress")
                view = view[written:]
        except OSError:
            path.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)
    except FileExistsError:
        pass
    except OSError as exc:
        logger.warning("[Go2/VisualTarget] frame capture failed: %s", type(exc).__name__)
        emit_agent_event("vision.observe", phase="frame_capture", saved=False,
                         frame_id=frame_id, reason="capture_write_failed",
                         error_type=type(exc).__name__)
        return None
    emit_agent_event("vision.observe", phase="frame_capture", saved=True,
                     frame_id=frame_id, artifact=str(path),
                     sha256=digest, size_bytes=len(jpeg))
    return path


def target_offset(observation: dict, frame: dict) -> tuple[float, float]:
    """Return horizontal image error and apparent height, both normalized."""
    if observation.get("found") is not True:
        emit_agent_event("vision.gate", gate="target_visible", passed=False,
                         frame_id=frame.get("frame_id"), found=observation.get("found"),
                         reason="target_not_visible")
        raise TargetUnlocalized("target_not_visible")
    box = observation.get("bbox_1000")
    width, height = frame.get("width"), frame.get("height")
    if (not isinstance(box, list) or len(box) != 4
            or not isinstance(width, int) or not isinstance(height, int)
            or width <= 0 or height <= 0
            or not all(type(value) in (int, float) and math.isfinite(value) for value in box)):
        emit_agent_event("vision.gate", gate="bbox", passed=False,
                         frame_id=frame.get("frame_id"), bbox_1000=box,
                         width=width, height=height, detail="invalid_shape_or_type",
                         reason="vision_result_invalid")
        logger.warning('[Go2/VisualTarget] invalid normalized bbox shape: bbox=%r frame=%rx%r',
                       box, width, height)
        raise TargetUnlocalized("vision_result_invalid")
    left, top, right, bottom = box
    if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
        emit_agent_event("vision.gate", gate="bbox", passed=False,
                         frame_id=frame.get("frame_id"), bbox_1000=box,
                         width=width, height=height, detail="outside_0_1000_or_degenerate",
                         reason="vision_result_invalid")
        logger.warning('[Go2/VisualTarget] bbox outside normalized 1000 range: bbox=%r frame=%rx%r',
                       box, width, height)
        raise TargetUnlocalized("vision_result_invalid")
    offset, apparent_height = (left + right - 1000) / 1000, (bottom - top) / 1000
    emit_agent_event("vision.gate", gate="target_geometry", passed=True,
                     frame_id=frame.get("frame_id"), bbox_1000=box,
                     bbox_pixels=[round(left * width / 1000, 1), round(top * height / 1000, 1),
                                  round(right * width / 1000, 1), round(bottom * height / 1000, 1)],
                     offset=round(offset, 4), apparent_height=round(apparent_height, 4))
    return offset, apparent_height


def forward_waypoint(grid, pose: dict) -> tuple[float, float]:
    """Pick a short straight segment with extra known-free space beyond it."""
    heading = pose["heading"]
    free_distance = _raycast(grid, pose["x"], pose["y"], heading)
    if free_distance < MIN_FORWARD_FREE_M:
        emit_agent_event("vision.gate", gate="forward_path", passed=False,
                         known_free_m=round(free_distance, 3), required_free_m=MIN_FORWARD_FREE_M,
                         reason="no_known_free_forward_path")
        raise TargetUnlocalized("no_known_free_forward_path")
    for distance in (0.0, 0.15, 0.3, STEP_M):
        x = pose["x"] + distance * math.cos(heading)
        y = pose["y"] + distance * math.sin(heading)
        if not grid.is_body_clear(x, y, heading):
            emit_agent_event("vision.gate", gate="body_clearance", passed=False,
                             distance_m=distance, reason="body_clearance_unavailable")
            raise TargetUnlocalized("body_clearance_unavailable")
    emit_agent_event("vision.gate", gate="forward_path", passed=True,
                     known_free_m=round(free_distance, 3), required_free_m=MIN_FORWARD_FREE_M,
                     step_m=STEP_M)
    return (pose["x"] + STEP_M * math.cos(heading),
            pose["y"] + STEP_M * math.sin(heading))


async def _observe_once(target: str) -> tuple[float, float, dict]:
    snapshot, reason = navigator._snapshot()
    if reason:
        now = time.monotonic()
        pose_at = getattr(go2, "odom_received_at", None)
        map_at = getattr(go2, "voxel_received_at", None)
        emit_agent_event("vision.observe", phase="map_preflight", passed=False,
                         reason=reason,
                         pose_age_s=round(now - pose_at, 3) if pose_at is not None else None,
                         map_age_s=round(now - map_at, 3) if map_at is not None else None)
        raise TargetUnlocalized(reason)
    pose, grid = snapshot
    captured = go2.latest_frame_snapshot()
    if captured is None:
        emit_agent_event("vision.observe", phase="frame_preflight", passed=False,
                         reason="frame_unavailable", pose=pose)
        raise TargetUnlocalized("frame_unavailable")
    jpeg, frame = captured
    now = time.monotonic()
    session_id = getattr(go2, "_odom_session_id", None)
    frame_age = (now - frame["received_at"]
                 if isinstance(frame.get("received_at"), (int, float)) else None)
    emit_agent_event("vision.observe", phase="frame_snapshot",
                     frame_id=frame.get("frame_id"), width=frame.get("width"),
                     height=frame.get("height"), frame_age_s=round(frame_age, 3)
                     if frame_age is not None else None,
                     map_frame=getattr(grid, "frame_id", None),
                     map_resolution_m=getattr(grid, "resolution", None),
                     map_has_free_space=getattr(grid, "has_free_space_evidence", None),
                     frame_odom_session=frame.get("odom_session_id"),
                     pose_odom_session=pose.get("session_id"),
                     current_odom_session=session_id,
                     pose_age_s=round(now - go2.odom_received_at, 3)
                     if getattr(go2, "odom_received_at", None) is not None else None,
                     map_age_s=round(now - go2.voxel_received_at, 3)
                     if getattr(go2, "voxel_received_at", None) is not None else None,
                     pose={key: pose.get(key) for key in ("x", "y", "heading")})
    if (not session_id or pose.get("session_id") != session_id
            or frame_age is None or not 0 <= frame_age <= MAX_FRAME_AGE_S
            or frame.get("odom_session_id") != session_id):
        emit_agent_event("vision.gate", gate="frame_freshness", passed=False,
                         frame_id=frame.get("frame_id"), frame_age_s=frame_age,
                         maximum_age_s=MAX_FRAME_AGE_S, reason="frame_stale")
        raise TargetUnlocalized("frame_stale")
    image_path = _save_diagnostic_frame(jpeg, frame)
    voxel_snapshot = getattr(go2, "voxel_raw", None)
    voxel_received_at = getattr(go2, "voxel_received_at", None)
    observation = await detect_visible_target(jpeg, target, frame)
    offset, height = target_offset(observation, frame)
    live, reason = navigator._snapshot()
    if reason or getattr(go2, "_odom_session_id", None) != session_id:
        emit_agent_event("vision.gate", gate="post_model_map", passed=False,
                         frame_id=frame.get("frame_id"), reason=reason or "odom_session_mismatch")
        raise TargetUnlocalized(reason or "odom_session_mismatch")
    fresh_pose, _ = live
    shift = math.hypot(fresh_pose["x"] - pose["x"], fresh_pose["y"] - pose["y"])
    turn = abs(math.atan2(math.sin(fresh_pose["heading"] - pose["heading"]),
                          math.cos(fresh_pose["heading"] - pose["heading"])))
    if shift > 0.05 or turn > 0.05:
        emit_agent_event("vision.gate", gate="pose_stability", passed=False,
                         frame_id=frame.get("frame_id"), translation_m=round(shift, 3),
                         rotation_rad=round(turn, 3), reason="pose_changed_during_detection")
        raise TargetUnlocalized("pose_changed_during_detection")
    emit_agent_event("vision.gate", gate="pose_stability", passed=True,
                     frame_id=frame.get("frame_id"), translation_m=round(shift, 3),
                     rotation_rad=round(turn, 3))
    save_paired_sample(image_path, frame, voxel_snapshot, voxel_received_at,
                       observation.get("bbox_1000"))
    return offset, height, fresh_pose


async def _observe(target: str) -> tuple[float, float, dict]:
    """Recapture after a pose change instead of steering from an old frame."""
    try:
        return await _observe_once(target)
    except TargetUnlocalized as exc:
        if exc.reason != "pose_changed_during_detection":
            raise
        emit_agent_event("vision.observe", phase="recapture_after_pose_change",
                         reason=exc.reason)
        return await _observe_once(target)


async def approach_visible_target(target: str) -> dict:
    """Turn toward the target, walk only mapped short segments, then stop."""
    posture_at = getattr(go2, "_sport_state_observed_at", None)
    posture_age = (time.monotonic() - posture_at
                   if isinstance(posture_at, (int, float)) else None)
    emit_agent_event("vision.gate", gate="approach_preflight", passed=(
        isinstance(target, str) and bool(target.strip()) and len(target) <= 100
        and bool(go2.is_connected) and getattr(go2, "fsm_state", None) == "standing"
        and posture_age is not None and 0 <= posture_age <= 1.0),
        connected=bool(go2.is_connected), fsm_state=getattr(go2, "fsm_state", None),
        posture_age_s=round(posture_age, 3) if posture_age is not None else None)
    if not isinstance(target, str) or not target.strip() or len(target) > 100:
        return {"result": "blocked", "reason": "invalid_target",
                "message": "请说明要接近的画面目标，本次未发出移动指令。"}
    if not go2.is_connected:
        return {"result": "blocked", "reason": "disconnected",
                "message": "Go2 当前未连接，本次未发出移动指令。"}
    if getattr(go2, "fsm_state", None) != "standing":
        return {"result": "blocked", "reason": "posture_not_confirmed",
                "message": "Go2 当前不是已确认的站立状态，本次未发出移动指令。"}
    if posture_age is None or not 0 <= posture_age <= 1.0:
        return {"result": "blocked", "reason": "posture_not_confirmed",
                "message": "Go2 的站立状态数据过期，本次未发出移动指令。"}
    moved = 0.0
    try:
        context = current_context()
        control_lease.acquire(context)
        steps = 0
        turns = 0
        previous_offset = None
        for _ in range(MAX_STEPS + MAX_TURNS + 1):
            offset, apparent_height, pose = await _observe(target)
            emit_agent_event("vision.observe", phase="decision", steps=steps, turns=turns,
                             offset=round(offset, 4), apparent_height=round(apparent_height, 4),
                             pose={key: pose.get(key) for key in ("x", "y", "heading")})
            if previous_offset is not None:
                improved = abs(offset) <= abs(previous_offset) - 0.03
                emit_agent_event("vision.gate", gate="turn_visual_confirmation",
                                 passed=improved, previous_offset=round(previous_offset, 4),
                                 current_offset=round(offset, 4), required_improvement=0.03,
                                 reason=None if improved else "visual_turn_not_confirmed")
                if not improved:
                    raise TargetUnlocalized("visual_turn_not_confirmed")
                previous_offset = None
            snapshot, reason = navigator._snapshot()
            if reason:
                emit_agent_event("vision.gate", gate="decision_map", passed=False,
                                 reason=reason)
                raise TargetUnlocalized(reason)
            current_pose, grid = snapshot
            if apparent_height >= 0.45:
                emit_agent_event("vision.gate", gate="apparent_close", passed=True,
                                 apparent_height=round(apparent_height, 4), threshold=0.45)
                break  # Close in the image; distance and arrival remain unconfirmed.
            if abs(offset) > CENTER_TOLERANCE:
                turn_details = (grid.turn_clearance_details(current_pose["x"], current_pose["y"])
                                if hasattr(grid, "turn_clearance_details") else
                                {"clear": grid.is_turn_clear(current_pose["x"], current_pose["y"])})
                turn_clear = turns < MAX_TURNS and turn_details["clear"]
                emit_agent_event("vision.gate", gate="turn_clearance", passed=turn_clear,
                                 turns=turns, maximum_turns=MAX_TURNS,
                                 map_evidence=turn_details,
                                 reason=None if turn_clear else "turn_clearance_unavailable")
                if turn_clear:
                    heading = current_pose["heading"] + math.copysign(TURN_RAD, -offset)
                    emit_agent_event("vision.motion", phase="request", kind="turn", turns=turns,
                                     from_pose={key: current_pose.get(key) for key in ("x", "y", "heading")},
                                     goal={"x": current_pose["x"], "y": current_pose["y"],
                                           "heading": heading}, speed_limit=0.15)
                    result = await navigator.go_to_coordinate(
                        current_pose["x"], current_pose["y"], owner_session_id=context.session_id,
                        heading=heading, speed_limit=0.15)
                    emit_agent_event("vision.motion", phase="result", kind="turn", turns=turns,
                                     status=result.get("status"), reason=result.get("reason"),
                                     task_id=result.get("task_id"), last_trusted_pose=result.get("last_trusted_pose"))
                    if result.get("status") != "succeeded":
                        status = "cancelled" if result.get("status") == "cancelled" else "blocked"
                        return {"result": status, "reason": result.get("reason"),
                                "navigation": result, "target": target,
                                "message": f"转向目标时停止：{result.get('message') or result.get('reason')}；尚未确认到达。"}
                    previous_offset = offset
                    turns += 1
                    continue
                # A target in the camera's central half can still be approached
                # straight ahead when a full in-place turn is not mapped. The
                # forward waypoint below independently checks the traversed map.
                front_target = abs(offset) < 0.5 and turns < MAX_TURNS
                emit_agent_event("vision.gate", gate="straight_approach_candidate",
                                 passed=front_target, offset=round(offset, 4),
                                 reason=None if front_target else "turn_clearance_unavailable")
                if not front_target:
                    raise TargetUnlocalized("turn_clearance_unavailable")
            if steps >= MAX_STEPS:
                emit_agent_event("vision.gate", gate="step_budget", passed=False,
                                 steps=steps, maximum_steps=MAX_STEPS)
                break
            x, y = forward_waypoint(grid, current_pose)
            emit_agent_event("vision.motion", phase="request", kind="forward", steps=steps,
                             from_pose={key: current_pose.get(key) for key in ("x", "y", "heading")},
                             goal={"x": x, "y": y}, speed_limit=0.15)
            result = await navigator.go_to_coordinate(
                x, y, owner_session_id=context.session_id, speed_limit=0.15)
            emit_agent_event("vision.motion", phase="result", kind="forward", steps=steps,
                             status=result.get("status"), reason=result.get("reason"),
                             task_id=result.get("task_id"), last_trusted_pose=result.get("last_trusted_pose"))
            if result.get("status") != "succeeded":
                status = "cancelled" if result.get("status") == "cancelled" else "blocked"
                last_pose = result.get("last_trusted_pose")
                if (isinstance(last_pose, dict)
                        and last_pose.get("session_id") == current_pose.get("session_id")
                        and all(isinstance(last_pose.get(key), (int, float))
                                and math.isfinite(last_pose[key]) for key in ("x", "y"))):
                    moved += math.hypot(last_pose["x"] - current_pose["x"],
                                        last_pose["y"] - current_pose["y"])
                reason_text = result.get("message") or result.get("reason") or "导航未完成"
                detail = (reason_text if reason_text.endswith("已停止") else f"因{reason_text}停止")
                return {"result": status, "reason": result.get("reason"),
                        "navigation": result, "target": target,
                        "approached_m": round(moved, 2),
                        "message": (f"已朝「{target}」方向前进约 {moved:.2f} 米，随后{detail}；尚未确认到达。"
                                    if moved > 0 else
                                    f"接近「{target}」时{detail}；本次未确认前进。")}
            final_pose = result.get("last_trusted_pose") or go2.odom
            displacement = math.hypot(final_pose["x"] - current_pose["x"],
                                      final_pose["y"] - current_pose["y"])
            emit_agent_event("vision.gate", gate="movement_confirmation",
                             passed=displacement >= 0.15,
                             displacement_m=round(displacement, 3), minimum_m=0.15,
                             reason=None if displacement >= 0.15 else "movement_unconfirmed")
            if displacement < 0.15:
                raise TargetUnlocalized("movement_unconfirmed")
            moved += displacement
            steps += 1
        if moved >= 0.15:
            return {"result": "ok", "target": target, "approached_m": round(moved, 2),
                    "confirmation": "odom_stopped_and_visual_reobserved",
                    "message": f"已朝「{target}」方向前进约 {moved:.1f} 米，并停下重新观察；尚未确认到达目标。"}
        return {"result": "blocked", "reason": "approach_not_possible", "target": target,
                "message": "目标看起来已很近或缺少可安全前进的空间，本次没有继续前进；尚未确认到达。"}
    except TargetUnlocalized as exc:
        reason_text = _REASONS.get(exc.reason, exc.reason)
        return {"result": "blocked", "reason": exc.reason, "target": target,
                "approached_m": round(moved, 2),
                "message": (f"已朝「{target}」方向前进约 {moved:.1f} 米，随后因{reason_text}停止；尚未确认到达。"
                            if moved >= 0.15 else
                            f"未能安全接近「{target}」：{reason_text}；本次未确认前进。")}
