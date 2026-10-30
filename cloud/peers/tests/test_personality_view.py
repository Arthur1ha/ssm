"""Peer-facing personality facades read the same saved profile as reply loops."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from cloud.agent_runtime.personality import ProfileStore
from cloud.esp32.agentcore import soul as lamp_soul
from cloud.esp32.agentcore.personality import profile as lamp_profile
from cloud.go2.agentcore import soul as go2_soul
from cloud.go2.agentcore.personality import profile as go2_profile
from cloud.space.execution import ExecutionContext


def _isolate_lamp(tmp_path, monkeypatch, migration=lambda: {}):
    path = tmp_path / "lamp" / "profile.json"
    monkeypatch.setattr(lamp_profile, "_store", ProfileStore(
        agent_id="esp32_desk_led", path=path, prompt=lamp_profile.DEFAULT_PROMPT,
        trait_specs=lamp_profile.TRAIT_SPECS, migration=migration))
    monkeypatch.setattr(lamp_soul, "emit_agent_event", lambda *_args, **_kwargs: None)
    return path


def _isolate_go2(tmp_path, monkeypatch):
    path = tmp_path / "go2" / "profile.json"
    monkeypatch.setattr(go2_profile, "_store", ProfileStore(
        agent_id="go2", path=path, prompt=go2_profile.DEFAULT_PROMPT,
        trait_specs=go2_profile.TRAIT_SPECS, migration=lambda: {}))
    monkeypatch.setattr(go2_soul, "emit_agent_event", lambda *_args, **_kwargs: None)
    return path


def test_lamp_first_start_keeps_saved_admin_profile(tmp_path, monkeypatch):
    path = _isolate_lamp(tmp_path, monkeypatch)
    lamp_soul.ensure_personality_profile()
    assert lamp_profile.get_profile()["traits"]["social_initiative"] == 70
    context = ExecutionContext("admin", "admin", True)
    lamp_soul.set_personality("管理员人设", context, 92)
    lamp_soul.ensure_personality_profile()
    assert lamp_soul.get_personality_view() == {
        "prompt": "管理员人设", "social_initiative": 92}
    assert json.loads(path.read_text(encoding="utf-8"))["prompt"] == "管理员人设"


def test_lamp_migrates_old_profile_once_and_preserves_score(tmp_path, monkeypatch):
    old_path = tmp_path / "old-lamp.json"
    old_path.write_text(json.dumps({"prompt": "旧人设", "social_initiative": 12}), encoding="utf-8")
    monkeypatch.setattr(lamp_profile, "PERSONALITY_FILE", old_path)
    _isolate_lamp(tmp_path, monkeypatch, migration=lamp_profile._migrate)
    assert lamp_soul.get_personality_view() == {"prompt": "旧人设", "social_initiative": 12}
    context = ExecutionContext("admin", "admin", True)
    lamp_soul.set_personality("新表达", context)
    assert lamp_soul.get_personality_view() == {"prompt": "新表达", "social_initiative": 12}


def test_go2_persona_and_extraversion_are_one_atomic_profile(tmp_path, monkeypatch):
    path = _isolate_go2(tmp_path, monkeypatch)
    go2_soul.set_personality("新表达", 10)
    before = path.read_bytes()
    assert go2_soul.get_personality_view() == {"prompt": "新表达", "social_initiative": 10}
    with pytest.raises(ValueError):
        go2_soul.set_personality("不应保存", 101)
    assert path.read_bytes() == before


def test_go2_daily_evolution_preserves_concurrent_admin_edit(tmp_path, monkeypatch):
    _isolate_go2(tmp_path, monkeypatch)
    go2_soul.set_personality("原始表达", 45)

    class Model:
        async def ainvoke(self, _messages):
            go2_soul.set_personality("管理员刚更新", 90)
            return SimpleNamespace(content='{"extraversion": 2}')

    monkeypatch.setattr(go2_soul, "get_text_llm", lambda: Model())
    asyncio.run(go2_soul.evolve_from_summary("2026-10-30", "有趣的一天"))
    assert go2_soul.get_personality_view() == {
        "prompt": "管理员刚更新", "social_initiative": 92}
