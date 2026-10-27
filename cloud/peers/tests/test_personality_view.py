import asyncio
import json
from types import SimpleNamespace

import pytest

from cloud.space.execution import ExecutionContext
from cloud.esp32.agentcore import soul as lamp_soul
from cloud.go2.agentcore import soul as go2_soul


def test_lamp_legacy_profile_and_score_preservation(tmp_path, monkeypatch):
    path = tmp_path / "lamp.json"
    path.write_text(json.dumps({"prompt": "旧人设"}), encoding="utf-8")
    monkeypatch.setattr(lamp_soul, "PERSONALITY_FILE", path)
    monkeypatch.setattr(lamp_soul, "emit_agent_event", lambda *a, **k: None)
    context = ExecutionContext("admin", "admin", True)
    assert lamp_soul.get_personality_view() == {"prompt": "旧人设", "social_initiative": 70}
    lamp_soul.set_personality("新表达", context, 12)
    lamp_soul.set_personality("再修改", context)
    assert lamp_soul.get_personality_view() == {"prompt": "再修改", "social_initiative": 12}
    with pytest.raises(ValueError):
        lamp_soul.set_personality("错误", context, 101)
    assert lamp_soul.get_personality_view()["prompt"] == "再修改"


def test_go2_profile_maps_extraversion_and_rolls_back_on_second_write(tmp_path, monkeypatch):
    prompt_path, traits_path = tmp_path / "personality.json", tmp_path / "traits.json"
    prompt_path.write_text(json.dumps({"prompt": "旧人设"}), encoding="utf-8")
    traits_path.write_text(json.dumps({"extraversion": 40, "curiosity": 70}), encoding="utf-8")
    monkeypatch.setattr(go2_soul, "_PERSONALITY_FILE", prompt_path)
    monkeypatch.setattr(go2_soul, "_TRAITS_FILE", traits_path)
    monkeypatch.setattr(go2_soul, "emit_agent_event", lambda *a, **k: None)
    assert go2_soul.get_personality_view() == {"prompt": "旧人设", "social_initiative": 40}
    go2_soul.set_personality("新表达", 10)
    go2_soul.set_personality("再修改")
    assert go2_soul.get_personality_view() == {"prompt": "再修改", "social_initiative": 10}
    before_prompt, before_traits = prompt_path.read_bytes(), traits_path.read_bytes()
    original = go2_soul._atomic_json

    def fail_traits(path, value):
        if path == traits_path:
            raise OSError("simulated trait write failure")
        return original(path, value)

    monkeypatch.setattr(go2_soul, "_atomic_json", fail_traits)
    with pytest.raises(OSError):
        go2_soul.set_personality("不应保存", 99)
    assert prompt_path.read_bytes() == before_prompt
    assert traits_path.read_bytes() == before_traits


def test_go2_daily_description_does_not_replace_new_admin_edit(tmp_path, monkeypatch):
    prompt_path, traits_path = tmp_path / "personality.json", tmp_path / "traits.json"
    monkeypatch.setattr(go2_soul, "_PERSONALITY_FILE", prompt_path)
    monkeypatch.setattr(go2_soul, "_TRAITS_FILE", traits_path)
    monkeypatch.setattr(go2_soul, "emit_agent_event", lambda *a, **k: None)
    go2_soul.set_personality("原始表达", 45)

    class Model:
        calls = 0

        async def ainvoke(self, _messages):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(content='{"extraversion": 2}')
            go2_soul.set_personality("管理员刚更新", 90)
            return SimpleNamespace(content="你是一只很活泼的机器狗。")

    model = Model()
    monkeypatch.setattr(go2_soul, "get_text_llm", lambda: model)
    asyncio.run(go2_soul.evolve_from_summary("2026-10-26", "有趣的一天", traits_path))
    assert go2_soul.get_personality_view() == {
        "prompt": "管理员刚更新", "social_initiative": 90}
