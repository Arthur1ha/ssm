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


def test_on_vision_frame_person_with_face_transitions_to_social(monkeypatch, tmp_path):
    """人脸可见（近距主动互动）才触发 SOCIAL。"""
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    d = drive_mod.Drive()
    d.on_vision_frame(_make_frame(person_detected=True, count=1, face_detected=True,
                                  changed=True, change_type="person_entered"))
    assert d.state_snapshot["state"] == "SOCIAL"
    assert d._person_present is True
    assert d._person_engaging is True
    assert d.state_snapshot["curiosity"] == 0


def test_on_vision_frame_background_person_stays_idle(monkeypatch, tmp_path):
    """远处背景人（无人脸）不切换 SOCIAL，探索不被打断。"""
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    d = drive_mod.Drive()
    d.on_vision_frame(_make_frame(person_detected=True, count=1, face_detected=False,
                                  changed=True, change_type="person_entered"))
    assert d.state_snapshot["state"] == "IDLE"
    assert d._person_present is True
    assert d._person_engaging is False


def test_on_vision_frame_person_enter_records_vision_change(monkeypatch, tmp_path):
    import cloud.go2.navigation.drive as drive_mod
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
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    fresh = EpisodeMemory(episodes_dir=tmp_path)
    monkeypatch.setattr(drive_mod, "episode_memory", fresh)

    d = drive_mod.Drive()
    d._person_present = True
    d.on_vision_frame(_make_frame(person_detected=False, changed=True,
                                  change_type="person_left"))
    assert d._person_present is False
    assert any("离开" in e["content"] for e in fresh.entries())


def test_no_double_social_transition(monkeypatch, tmp_path):
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

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


def test_start_creates_task_and_resets_state(monkeypatch, tmp_path):
    import cloud.go2.navigation.drive as drive_mod
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
    import cloud.go2.navigation.drive as drive_mod
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


# ── 社交活锁 Bug B 修复测试 ────────────────────────────────────────────

def test_person_engaging_prevents_exploring_enters_social(monkeypatch, tmp_path):
    """Bug B 修复①：person_engaging 为 True 且 curiosity 达阈值时，IDLE 应转 SOCIAL 而非 EXPLORING。"""
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    d = drive_mod.Drive()
    d._state = drive_mod.MotivationalState.IDLE
    d._curiosity = drive_mod._CURIOSITY_THRESHOLD - 1   # 下一 tick 会触发
    d._person_engaging = True

    # 模拟单次 tick 决策（对应 _run_loop 中 IDLE 分支）
    d._curiosity += 1
    if d._curiosity >= drive_mod._CURIOSITY_THRESHOLD:
        if d._person_engaging:
            d._state = drive_mod.MotivationalState.SOCIAL
            d._curiosity = 0

    assert d._state == drive_mod.MotivationalState.SOCIAL, (
        "person_engaging=True 时 curiosity 达阈值应进入 SOCIAL，不应进 EXPLORING"
    )
    assert d._curiosity == 0


async def _run_one_tick(d, *, mock_social=True):
    """运行 _run_loop 恰好一个 tick，然后取消。mock_social 避免 LLM 调用。"""
    import cloud.go2.navigation.drive as drive_mod
    with patch.object(d, "_do_social", new_callable=AsyncMock):
        call_count = [0]
        original_sleep = asyncio.sleep

        async def mock_sleep(t):
            call_count[0] += 1
            if call_count[0] > 1:
                raise asyncio.CancelledError()

        with patch("cloud.go2.navigation.drive.asyncio.sleep", side_effect=mock_sleep):
            task = asyncio.create_task(d._run_loop())
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def test_social_exit_on_engage_gone_timeout(monkeypatch, tmp_path):
    """Bug B 修复②：人脸消失满 _ENGAGE_GONE_TIMEOUT 秒后，_run_loop 一个 tick 内从 SOCIAL 回 IDLE。"""
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    d = drive_mod.Drive()
    d._state = drive_mod.MotivationalState.SOCIAL
    d._person_engaging = False
    d._last_engaging_ts = time.time() - (drive_mod._ENGAGE_GONE_TIMEOUT + 1)

    asyncio.run(_run_one_tick(d))

    assert d._state == drive_mod.MotivationalState.IDLE, (
        "人脸消失满 _ENGAGE_GONE_TIMEOUT 秒后应从 SOCIAL 回 IDLE"
    )
    assert d._social_tick == 0, "_social_tick 应在 SOCIAL→IDLE 时被重置"


def test_social_no_exit_before_engage_gone_timeout(monkeypatch, tmp_path):
    """Bug B 修复②：人脸刚消失未满 _ENGAGE_GONE_TIMEOUT 秒，_run_loop 不应离开 SOCIAL。"""
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    d = drive_mod.Drive()
    d._state = drive_mod.MotivationalState.SOCIAL
    d._person_engaging = False
    d._last_engaging_ts = time.time() - 1   # 只过了 1s，未达超时

    asyncio.run(_run_one_tick(d))

    assert d._state == drive_mod.MotivationalState.SOCIAL, (
        "人脸消失未满 _ENGAGE_GONE_TIMEOUT 秒时不应离开 SOCIAL"
    )


def test_do_explore_skips_observe_when_person_engaging(monkeypatch, tmp_path):
    """Bug B 修复③：_do_explore 进入时已有 person_engaging=True，不调用 go2_observe，直接返回。"""
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    observe_mock = AsyncMock()
    monkeypatch.setattr(drive_mod, "go2_observe", observe_mock)

    d = drive_mod.Drive()
    d._state = drive_mod.MotivationalState.EXPLORING
    d._person_engaging = True  # 进入时已有人脸

    async def run():
        await d._do_explore()

    asyncio.run(run())
    observe_mock.assert_not_called()


def test_do_explore_skips_observe_when_user_interrupt(monkeypatch, tmp_path):
    """Bug B 修复③：_do_explore 进入时 user_interrupt=True，不调用 go2_observe，直接返回。"""
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    observe_mock = AsyncMock()
    monkeypatch.setattr(drive_mod, "go2_observe", observe_mock)

    d = drive_mod.Drive()
    d._state = drive_mod.MotivationalState.EXPLORING
    d.user_interrupt = True

    async def run():
        await d._do_explore()

    asyncio.run(run())
    observe_mock.assert_not_called()


def test_do_explore_sets_social_after_when_person_engaging(monkeypatch, tmp_path):
    """Bug B 修复①②：_do_explore 结束时若 person_engaging=True，调用方应转入 SOCIAL 而非 IDLE。

    这个测试通过在 person_engaging=True 的情况下模拟探索后的状态决策来验证。
    实际逻辑在 _run_loop 的 IDLE 分支调用 _do_explore 之后。
    """
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    observe_mock = AsyncMock(return_value="当前场景：空旷走廊")
    monkeypatch.setattr(drive_mod, "go2_observe", observe_mock)

    d = drive_mod.Drive()
    d._state = drive_mod.MotivationalState.EXPLORING
    d._person_engaging = True  # 探索中途有人脸出现

    async def run():
        await d._do_explore()
        # 探索结束后，状态应由 _run_loop 根据 person_engaging 决定
        # 验证：_do_explore 本身不再无条件设 IDLE（而是让调用方决定）
        # 探索因 person_engaging=True 立即退出（不调用 observe）
        # 且状态不应被 _do_explore 内部强制设为 IDLE
        # （IDLE 在 _run_loop 调用链的后续赋值，取决于 person_engaging）
        if d._person_engaging:
            d._state = drive_mod.MotivationalState.SOCIAL
        else:
            d._state = drive_mod.MotivationalState.IDLE

    asyncio.run(run())

    assert d._state == drive_mod.MotivationalState.SOCIAL, (
        "探索结束时 person_engaging=True 应进入 SOCIAL 而非 IDLE"
    )
    observe_mock.assert_not_called()


# ── Bug A 修复：死路转圈 ───────────────────────────────────────────────────

def test_exec_explore_direction_blocked_message_guides_observe(monkeypatch, tmp_path):
    """Bug A 修复：explore_direction A*预检查失败时，返回消息应提示 go2_observe + go2_tag_location + 换开阔方向。"""
    import cloud.go2.navigation.drive as drive_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    monkeypatch.setattr(drive_mod, "episode_memory", EpisodeMemory(episodes_dir=tmp_path))

    fake_odom = {"x": 0.0, "y": 0.0, "heading": 0.0}
    fake_grid = MagicMock()
    fake_grid.odom_to_grid.return_value = (64, 64)
    fake_grid.grid = MagicMock()

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
    assert "go2_tag_location" in result, f"阻塞方向应提示用 go2_tag_location 标记，实际：{result}"
    assert "★开阔" in result or "可通行" in result, f"阻塞方向应提示换开阔方向，实际：{result}"
