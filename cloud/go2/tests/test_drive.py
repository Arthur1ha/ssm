# cloud/go2/tests/test_drive.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import time
import asyncio
import pytest
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
    from cloud.go2.navigation.drive import Drive
    d = Drive()
    assert d.state_snapshot["state"] == "IDLE"
    assert d.state_snapshot["curiosity"] == 0


def test_on_vision_frame_person_with_face_transitions_to_social(monkeypatch):
    """人脸可见（近距主动互动）才触发 SOCIAL。"""
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory())

    d = drive_mod.Drive()
    d.on_vision_frame(_make_frame(person_detected=True, count=1, face_detected=True,
                                  changed=True, change_type="person_entered"))
    assert d.state_snapshot["state"] == "SOCIAL"
    assert d._person_present is True
    assert d._person_engaging is True
    assert d.state_snapshot["curiosity"] == 0


def test_on_vision_frame_background_person_stays_idle(monkeypatch):
    """远处背景人（无人脸）不切换 SOCIAL，探索不被打断。"""
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory())

    d = drive_mod.Drive()
    d.on_vision_frame(_make_frame(person_detected=True, count=1, face_detected=False,
                                  changed=True, change_type="person_entered"))
    assert d.state_snapshot["state"] == "IDLE"
    assert d._person_present is True
    assert d._person_engaging is False


def test_on_vision_frame_person_enter_records_vision_change(monkeypatch):
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    fresh = EpisodeMemory()
    monkeypatch.setattr(drive_mod, "episode_memory", fresh)

    d = drive_mod.Drive()
    d.on_vision_frame(_make_frame(person_detected=True, count=2, changed=True,
                                  change_type="person_entered"))
    entries = fresh.entries()
    assert len(entries) == 1
    assert "进入画面" in entries[0]["content"]


def test_on_vision_frame_person_leaves_records_vision_change(monkeypatch):
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    fresh = EpisodeMemory()
    monkeypatch.setattr(drive_mod, "episode_memory", fresh)

    d = drive_mod.Drive()
    d._person_present = True
    d.on_vision_frame(_make_frame(person_detected=False, changed=True,
                                  change_type="person_left"))
    assert d._person_present is False
    assert any("离开" in e["content"] for e in fresh.entries())


def test_no_double_social_transition(monkeypatch):
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory())

    d = drive_mod.Drive()
    frame = _make_frame(person_detected=True, count=1, face_detected=True,
                        changed=True, change_type="person_entered")
    d.on_vision_frame(frame)
    d.on_vision_frame(frame)  # 第二次调用不应重复触发
    assert d.state_snapshot["state"] == "SOCIAL"


def test_state_snapshot_contains_required_fields():
    from cloud.go2.navigation.drive import Drive
    snap = Drive().state_snapshot
    for key in ("state", "curiosity", "person_present", "last_action_ts"):
        assert key in snap


def test_start_creates_task_and_resets_state(monkeypatch):
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory())

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


def test_stop_cancels_task(monkeypatch):
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory())

    d = drive_mod.Drive()

    async def run():
        d.start()
        await asyncio.sleep(0)
        d.stop()
        assert d._task is None

    asyncio.run(run())


# ── Router endpoint tests ────────────────────────────────────────────

from fastapi import FastAPI
from fastapi.testclient import TestClient
from cloud.go2.router import router as go2_router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(go2_router)
    return TestClient(app)


def test_drive_state_endpoint_returns_expected_shape(client):
    r = client.get("/api/go2/drive/state")
    assert r.status_code == 200
    data = r.json()
    for key in ("state", "curiosity", "person_present", "last_action_ts"):
        assert key in data


def test_memory_endpoint_returns_entries_list(client):
    r = client.get("/api/go2/memory")
    assert r.status_code == 200
    data = r.json()
    assert "entries" in data
    assert isinstance(data["entries"], list)


def test_personality_get_returns_prompt(client):
    r = client.get("/api/go2/personality")
    assert r.status_code == 200
    assert "prompt" in r.json()


def test_personality_post_updates_system_prompt(client, tmp_path, monkeypatch):
    import cloud.go2.agentcore.soul as pers_mod
    monkeypatch.setattr(pers_mod, "_PERSONALITY_FILE", tmp_path / "p.json")
    r = client.post("/api/go2/personality", json={"prompt": "严肃、话少的机器狗"})
    assert r.status_code == 200
    assert pers_mod.get_system_prompt() == "严肃、话少的机器狗"
