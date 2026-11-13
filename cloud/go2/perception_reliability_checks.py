"""Fresh camera evidence, recoverable identity calls and bounded CPU image work."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cv2
import numpy as np
import pytest
from PIL import Image

from cloud.go2.identity_threshold_checks import recognition
from cloud.go2.connection.video import Go2Video, JPEG_QUALITY, ENCODE_INTERVAL_S
from cloud.go2.perception import vision
from cloud.space.people.provider import FaceCandidate, FaceProviderUnavailable, FaceSearchResult


def match():
    return FaceSearchResult((FaceCandidate("owner", "主人", 80),))


async def test_provider_failure_keeps_configuration_and_automatically_recovers(recognition):
    skill, provider, _, frame = recognition
    provider.check_group = AsyncMock(side_effect=[FaceProviderUnavailable("timeout", "test"), None])
    provider.search_persons.return_value = match()
    assert not await skill.check_provider()
    assert skill.enabled and not skill.available
    # A new appearance during the backoff must not hammer the cloud service.
    skill.on_vision_frame(frame)
    assert skill._auto_task is None
    assert skill.current.status == "error"
    assert provider.check_group.await_count == 1
    skill._health_retry_at = 0
    _, jpeg = skill._frame_getter()
    frame = {**frame, "frame_id": 2, "ts": time.time()}
    skill._frame_getter = lambda: (frame, jpeg)
    skill.on_vision_frame(frame)
    await skill._auto_task
    assert skill.available and skill.current.status == "recognized"
    assert provider.check_group.await_count == 2


async def test_old_frame_never_calls_identity_provider(recognition):
    skill, provider, _, frame = recognition
    frame["ts"] -= 3600
    result = await skill.recognize_now()
    assert (result.status, result.error_code) == ("error", "stale_frame")
    provider.search_persons.assert_not_awaited()


async def test_identity_uses_capture_time_and_expires_without_new_frames(recognition):
    skill, provider, _, frame = recognition
    provider.search_persons.return_value = match()
    result = await skill.recognize_now()
    assert (result.ts, result.frame_id) == (frame["ts"], frame["frame_id"])
    frame["ts"] -= 6
    assert skill.current.status == "idle"
    assert not skill.context()["person_id"]


@pytest.mark.parametrize("change", ["multiple", "absent", "moved", "session"])
async def test_face_discontinuity_clears_old_identity(recognition, change):
    skill, provider, _, frame = recognition
    provider.search_persons.return_value = match()
    await skill.recognize_now()
    skill._last_frame = dict(frame)
    skill._previous_face_count = 1
    new = {**frame, "ts": time.time(), "frame_id": 2}
    if change in {"multiple", "absent"}:
        count = 2 if change == "multiple" else 0
        new["faces"] = {"count": count, "boxes": [(30, 30, 100, 100)] * count}
    elif change == "moved":
        new["faces"] = {"count": 1, "boxes": [(500, 500, 100, 100)]}
    else:
        new["camera_session_id"] = "new-camera"
    # Block automatic work so the immediate invalidation can be inspected.
    skill._health_retry_at = time.time() + 5
    skill.on_vision_frame(new)
    assert skill.current.status == "idle"


async def test_late_provider_error_cannot_overwrite_disconnection(recognition):
    skill, provider, _, _ = recognition
    entered, release = asyncio.Event(), asyncio.Event()
    async def search(_):
        entered.set()
        await release.wait()
        raise FaceProviderUnavailable("timeout", "test")
    provider.search_persons.side_effect = search
    task = asyncio.create_task(skill.recognize_now())
    await entered.wait()
    skill.clear_transient()
    release.set()
    assert (await task).status == "idle"


async def test_provider_result_for_expired_capture_is_discarded(recognition):
    skill, provider, _, frame = recognition
    async def search(_):
        frame["ts"] -= 6
        return match()
    provider.search_persons.side_effect = search
    assert (await skill.recognize_now()).status == "idle"


async def test_frozen_and_replayed_camera_frame_do_not_become_new_observations(monkeypatch):
    processed = []
    def detect(jpeg, previous, frame_id):
        processed.append(frame_id)
        return {"frame_id": frame_id, "ts": time.time(),
                "persons": {"count": 0}, "faces": {"count": 0}}
    monkeypatch.setattr(vision, "detect", detect)
    loop = vision.VisionLoop()
    observed_at = time.time() - 1
    source = (b"jpeg", {"frame_id": 10, "odom_session_id": "camera", "captured_at": observed_at})
    await loop._sample(source)
    await loop._sample(source)
    await loop._sample((source[0], {**source[1], "frame_id": 9}))
    assert len(processed) == 1
    assert loop.latest["ts"] == observed_at
    assert loop.latest["source_frame_id"] == 10
    assert len(loop._frames) == 1
    loop.latest["ts"] -= 6
    assert loop.latest_frame_pair() == (None, None)


def test_face_detection_preserves_aspect_and_more_detail_than_body(monkeypatch):
    shapes = {}
    class Body:
        def detectMultiScale(self, img, **kwargs):
            shapes["body"] = img.shape[:2]
            return (), ()
    class Face:
        def detectMultiScale(self, img, **kwargs):
            shapes["face"] = img.shape[:2]
            return [(100, 50, 80, 80)]
    monkeypatch.setattr(vision, "_get_hog", Body)
    monkeypatch.setattr(vision, "_get_face_cascade", Face)
    _, jpeg = cv2.imencode(".jpg", np.zeros((720, 1280, 3), dtype=np.uint8))
    result = vision.detect(jpeg.tobytes())
    assert shapes == {"body": (180, 320), "face": (360, 640)}
    assert result["faces"]["boxes"] == [(200, 100, 160, 160)]


async def test_video_keeps_native_dimensions_and_high_quality_encoding(monkeypatch):
    robot = Go2Video()
    robot.is_connected = True
    robot._odom_session_id = "camera"
    image = Image.new("RGB", (640, 360))
    original_save = Image.Image.save
    qualities = []
    def save(img, fp, **kwargs):
        qualities.append(kwargs["quality"])
        return original_save(img, fp, **kwargs)
    monkeypatch.setattr(Image.Image, "save", save)
    async def recv():
        if qualities:
            raise EOFError("end test")
        return SimpleNamespace(to_image=lambda: image)
    await robot._consume_video(SimpleNamespace(recv=recv))
    jpeg, meta = robot.latest_frame_snapshot()
    decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape[:2] == (360, 640)
    assert qualities == [90] and JPEG_QUALITY == 90 and ENCODE_INTERVAL_S == .1
    assert (meta["width"], meta["height"]) == (640, 360)


def test_chat_reads_fresh_identity_projection(recognition, monkeypatch):
    from cloud.agents.go2 import prompts
    skill, _, person, frame = recognition
    skill._identity_frame = frame
    skill._current = skill._recognized(person, 80, frame)
    monkeypatch.setattr(prompts, "identity_skill", skill)
    monkeypatch.setattr(prompts, "get_profile", lambda: {"traits": {}})
    monkeypatch.setattr(prompts, "agent_names", lambda: {})
    monkeypatch.setattr(prompts, "persona_reply_prompt", lambda *a: "test")
    monkeypatch.setattr(prompts, "prompt_space_context", lambda: "")
    monkeypatch.setattr(prompts, "decision_context", lambda *a, **k: {})
    monkeypatch.setattr(prompts, "role_messages", lambda *a, context, **k: context)
    run = SimpleNamespace(origin_kind="user_delegated", goal="你认得我吗")
    assert prompts.build_messages(run)["identity"]["person_id"] == "owner"
    frame["ts"] -= 6
    assert prompts.build_messages(run)["identity"]["status"] == "idle"


def test_manual_mode_keeps_identity_and_teaching_consumers(monkeypatch):
    from unittest.mock import MagicMock
    from cloud.go2 import service
    loop = MagicMock()
    drive = MagicMock()
    monkeypatch.setattr(service, "vision_loop", loop)
    monkeypatch.setattr(service, "drive", drive)
    monkeypatch.setattr(service, "identity_skill", SimpleNamespace(enabled=True))
    monkeypatch.setattr(service.go2, "is_connected", True)
    for name in ("_active_rule_cb", "_active_drive_cb", "_active_identity_cb"):
        monkeypatch.setattr(service, name, None)
    service._configure_vision_consumers("manual")
    loop.add_callback.assert_any_call(service._on_identity_vision_frame)
    loop.add_callback.assert_any_call(service.person_teachings.on_vision_frame)
    assert loop.add_callback.call_count == 2
    loop.start.assert_called_once()
    drive.start.assert_not_called()


def test_expired_camera_presence_is_not_live_drive_state(monkeypatch):
    from cloud.go2.mode.free_explore import Drive
    drive = Drive()
    drive._person_present = True
    drive._last_vision_ts = time.time() - 6
    assert drive.state_snapshot["person_present"] is False


def test_identity_expiry_does_not_invent_person_departure(monkeypatch):
    from unittest.mock import MagicMock
    from cloud.go2 import service
    from cloud.go2.perception.identity import IdentityEvent, IdentitySnapshot
    from cloud.space.execution import ExecutionContext, use_context
    memory = MagicMock()
    monkeypatch.setattr(service, "episode_memory", memory)
    monkeypatch.setattr(service, "person_teachings", MagicMock())
    monkeypatch.setattr(service, "drive", MagicMock())
    event = IdentityEvent(IdentitySnapshot("recognized", "owner", "主人", "owner", 80, time.time()),
                          IdentitySnapshot("idle", error_code="identity_expired"))
    with use_context(ExecutionContext("admin-read", "admin", True)):
        service._on_identity_event(event)
    memory.add.assert_not_called()
