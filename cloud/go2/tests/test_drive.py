# cloud/go2/tests/test_drive.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import time
import asyncio
import httpx
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _make_frame(person_detected: bool, count: int = 0,
                face_detected: bool = False,
                changed: bool = False, change_type: str = "none") -> dict:
    return {
        "ts": time.time(),
        "persons": {"detected": person_detected, "count": count},
        "faces":   {"detected": face_detected, "count": 1 if face_detected else 0},
        "changed":     changed,
        "change_type": change_type,
    }


def test_initial_state_is_idle():
    from cloud.go2.agentcore.mode.free_explore import Drive
    d = Drive()
    assert d.state_snapshot["state"] == "IDLE"
    assert d.state_snapshot["curiosity"] == 0


def test_on_vision_frame_face_updates_social_context_without_stopping_exploration(monkeypatch, tmp_path):
    """人脸信号可用于互动，但不控制地图探索状态。"""
    import cloud.go2.agentcore.mode.free_explore as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    d = drive_mod.Drive()
    d.on_vision_frame(_make_frame(person_detected=True, count=1, face_detected=True,
                                  changed=True, change_type="person_entered"))
    assert d.state_snapshot["state"] == "IDLE"
    assert d._person_present is True
    assert d._person_engaging is True
    assert d.state_snapshot["curiosity"] == 0


def test_on_vision_frame_background_person_stays_idle(monkeypatch, tmp_path):
    """远处背景人（无人脸）不切换 SOCIAL，探索不被打断。"""
    import cloud.go2.agentcore.mode.free_explore as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    d = drive_mod.Drive()
    d.on_vision_frame(_make_frame(person_detected=True, count=1, face_detected=False,
                                  changed=True, change_type="person_entered"))
    assert d.state_snapshot["state"] == "IDLE"
    assert d._person_present is True
    assert d._person_engaging is False


def test_on_vision_frame_person_enter_records_vision_change(monkeypatch, tmp_path):
    import cloud.go2.agentcore.mode.free_explore as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    fresh = EpisodeMemory(episodes_dir=tmp_path)
    monkeypatch.setattr(drive_mod, "episode_memory", fresh)

    d = drive_mod.Drive()
    d.on_vision_frame(_make_frame(person_detected=True, count=2, changed=True,
                                  change_type="person_entered"))
    entries = fresh.entries()
    assert len(entries) == 1
    assert "进入画面" in entries[0]["content"]


def test_on_vision_frame_person_leaves_records_vision_change(monkeypatch, tmp_path):
    import cloud.go2.agentcore.mode.free_explore as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    fresh = EpisodeMemory(episodes_dir=tmp_path)
    monkeypatch.setattr(drive_mod, "episode_memory", fresh)

    d = drive_mod.Drive()
    d._person_present = True
    d.on_vision_frame(_make_frame(person_detected=False, changed=True,
                                  change_type="person_left"))
    assert d._person_present is False
    assert any("离开" in e["content"] for e in fresh.entries())


def test_repeated_face_frames_do_not_take_over_drive_state(monkeypatch, tmp_path):
    import cloud.go2.agentcore.mode.free_explore as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    d = drive_mod.Drive()
    frame = _make_frame(person_detected=True, count=1, face_detected=True,
                        changed=True, change_type="person_entered")
    d.on_vision_frame(frame)
    d.on_vision_frame(frame)  # 第二次调用不应重复触发
    assert d.state_snapshot["state"] == "IDLE"


def test_state_snapshot_contains_required_fields():
    from cloud.go2.agentcore.mode.free_explore import Drive
    snap = Drive().state_snapshot
    for key in ("state", "curiosity", "person_present", "last_action_ts"):
        assert key in snap


def test_start_creates_task_and_resets_state(monkeypatch, tmp_path):
    import cloud.go2.agentcore.mode.free_explore as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    d = drive_mod.Drive()
    d._curiosity = 99
    d._state = drive_mod.MotivationalState.SOCIAL

    async def run():
        d.start()
        await asyncio.sleep(0)
        assert d._task is not None
        assert d.state_snapshot["state"] == "IDLE"
        assert d.state_snapshot["curiosity"] == 0
        d.stop()

    asyncio.run(run())


def test_stop_cancels_task(monkeypatch, tmp_path):
    import cloud.go2.agentcore.mode.free_explore as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    d = drive_mod.Drive()

    async def run():
        d.start()
        await asyncio.sleep(0)
        d.stop()
        assert d._task is None

    asyncio.run(run())


# ── Router endpoint tests ────────────────────────────────────────────

from fastapi import FastAPI
from cloud.go2.router import router as go2_router


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "test-service-token")
    app = FastAPI()
    app.include_router(go2_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={
            "X-SSM-Service-Token": "test-service-token",
            "X-SSM-Session-Id": "test-admin",
            "X-SSM-Actor": "admin",
            "X-SSM-Persist": "true",
        },
    ) as value:
        yield value


@pytest.mark.asyncio
async def test_drive_state_endpoint_returns_expected_shape(client):
    r = await client.get("/api/go2/drive/state")
    assert r.status_code == 200
    data = r.json()
    for key in ("state", "curiosity", "person_present", "last_action_ts"):
        assert key in data


@pytest.mark.asyncio
async def test_memory_endpoint_returns_taught_and_today(client):
    r = await client.get("/api/go2/memory")
    assert r.status_code == 200
    data = r.json()
    assert "taught" in data
    assert isinstance(data["taught"], list)
    assert "today" in data
    assert isinstance(data["today"], str)


@pytest.mark.asyncio
async def test_personality_get_returns_prompt(client):
    r = await client.get("/api/go2/personality")
    assert r.status_code == 200
    assert "prompt" in r.json()


@pytest.mark.asyncio
async def test_personality_post_updates_system_prompt(client, tmp_path, monkeypatch):
    import cloud.go2.agentcore.soul as pers_mod
    monkeypatch.setattr(pers_mod, "_PERSONALITY_FILE", tmp_path / "p.json")
    r = await client.post("/api/go2/personality", json={"prompt": "严肃、话少的机器狗"})
    assert r.status_code == 200
    assert pers_mod.get_system_prompt() == "严肃、话少的机器狗"


# ── 视觉与地图探索独立 ──────────────────────────────────────────────


def test_face_signal_does_not_prevent_curiosity_exploration():
    from cloud.go2.agentcore.mode.free_explore import Drive, _CURIOSITY_THRESHOLD

    drive = Drive()
    drive._person_engaging = True
    drive._curiosity = _CURIOSITY_THRESHOLD - 1

    async def run_one_tick():
        with patch.object(drive, "_evaluate_social", new_callable=AsyncMock), \
             patch.object(drive, "_do_social", new_callable=AsyncMock) as social, \
             patch.object(drive, "_do_explore", new_callable=AsyncMock) as explore:
            count = 0
            async def one_sleep(_seconds):
                nonlocal count
                count += 1
                if count > 1:
                    raise asyncio.CancelledError()
            with patch("cloud.go2.agentcore.mode.free_explore.asyncio.sleep", side_effect=one_sleep):
                with pytest.raises(asyncio.CancelledError):
                    await drive._run_loop()
            explore.assert_awaited_once()
            social.assert_not_awaited()
            assert drive.state_snapshot["state"] == "IDLE"

    asyncio.run(run_one_tick())


def test_face_signal_does_not_skip_frontier_navigation(monkeypatch):
    import cloud.go2.agentcore.mode.free_explore as drive_mod
    from cloud.go2.navigation.navigator import navigator

    drive = drive_mod.Drive()
    drive._person_engaging = True
    monkeypatch.setattr(drive_mod.go2, "is_connected", True)
    monkeypatch.setattr(navigator, "_snapshot", lambda: ((
        {"x": 0.0, "y": 0.0, "heading": 0.0}, object()), None))
    target = (0.8, 0.0)
    monkeypatch.setattr(drive_mod.frontier_mod, "find_frontier_target",
                        MagicMock(side_effect=[target, None]))
    drive._navigate = AsyncMock(return_value={"result": "blocked", "reason": "no_known_free_path"})
    asyncio.run(drive._do_explore())
    drive._navigate.assert_awaited_once_with(coordinate=target)


def test_invalid_social_action_never_reaches_motion_executor(monkeypatch):
    import cloud.go2.agentcore.mode.free_explore as drive_mod
    from types import SimpleNamespace

    drive = drive_mod.Drive()
    monkeypatch.setattr(drive_mod, "go2_observe", AsyncMock(return_value="画面里没有人"))
    monkeypatch.setattr(drive_mod, "get_text_llm", lambda: SimpleNamespace(
        ainvoke=AsyncMock(return_value=SimpleNamespace(
            content='{"action":"go2_sport","reason":"试着探索"}'))))
    drive._execute_motion = AsyncMock()
    asyncio.run(drive._do_social())
    drive._execute_motion.assert_not_awaited()


def test_user_interrupt_prevents_map_exploration(monkeypatch):
    import cloud.go2.agentcore.mode.free_explore as drive_mod
    from cloud.go2.navigation.navigator import navigator

    drive = drive_mod.Drive()
    drive.user_interrupt = True
    snapshot = MagicMock()
    monkeypatch.setattr(navigator, "_snapshot", snapshot)
    asyncio.run(drive._do_explore())
    snapshot.assert_not_called()


# ── Bug A 修复：死路转圈 ───────────────────────────────────────────────────

def test_exec_explore_direction_blocked_message_guides_observe(monkeypatch, tmp_path):
    """Bug A 修复：explore_direction A*预检查失败时，返回消息应提示 go2_observe + go2_tag_location + 换开阔方向。"""
    import cloud.go2.agentcore.mode.free_explore as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    fake_odom = {"x": 0.0, "y": 0.0, "heading": 0.0}
    fake_grid = MagicMock()
    fake_grid.odom_to_grid.return_value = (64, 64)
    fake_grid.grid = MagicMock()
    from cloud.go2.navigation.navigator import navigator
    monkeypatch.setattr(navigator, "_snapshot", lambda: ((fake_odom, fake_grid), None))

    # go2.odom/occupancy_grid 是 property，用 patch 而非 monkeypatch
    with patch.object(type(drive_mod.go2), "odom",
                      new_callable=lambda: property(lambda self: fake_odom)):
        with patch.object(type(drive_mod.go2), "occupancy_grid",
                          new_callable=lambda: property(lambda self: fake_grid)):
            monkeypatch.setattr(drive_mod.frontier_mod, "find_exploration_target",
                                MagicMock(return_value=(1.0, 0.0)))

            with patch("cloud.go2.navigation.astar.astar", return_value=[]):
                with patch("cloud.go2.navigation.navigator._nearest_free_cell",
                           return_value=(65, 64)):
                    d = drive_mod.Drive()
                    result = asyncio.run(d._exec_explore_direction("forward"))

    assert "go2_observe" in result, f"阻塞方向应提示用 go2_observe 远观，实际：{result}"
    assert "go2_tag_location" not in result, f"远处兴趣点不能保存为当前位置，实际：{result}"
    assert "★开阔" in result or "可通行" in result, f"阻塞方向应提示换开阔方向，实际：{result}"
