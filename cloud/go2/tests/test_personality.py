# cloud/go2/tests/test_personality.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import json


def test_get_system_prompt_returns_default_when_file_missing(tmp_path, monkeypatch):
    import cloud.go2.personality as mod
    monkeypatch.setattr(mod, "_PERSONALITY_FILE", tmp_path / "no.json")
    assert mod.get_system_prompt() == mod._DEFAULT_PERSONALITY


def test_set_and_get_personality_roundtrip(tmp_path, monkeypatch):
    import cloud.go2.personality as mod
    monkeypatch.setattr(mod, "_PERSONALITY_FILE", tmp_path / "p.json")
    mod.set_personality("严肃的机器狗")
    assert mod.get_system_prompt() == "严肃的机器狗"


def test_set_personality_writes_valid_json(tmp_path, monkeypatch):
    import cloud.go2.personality as mod
    p = tmp_path / "p.json"
    monkeypatch.setattr(mod, "_PERSONALITY_FILE", p)
    mod.set_personality("测试性格")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["prompt"] == "测试性格"


def test_get_system_prompt_returns_latest_after_two_sets(tmp_path, monkeypatch):
    import cloud.go2.personality as mod
    monkeypatch.setattr(mod, "_PERSONALITY_FILE", tmp_path / "p.json")
    mod.set_personality("第一版")
    mod.set_personality("第二版")
    assert mod.get_system_prompt() == "第二版"
