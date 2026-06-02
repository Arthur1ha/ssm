import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import json
import time
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import asyncio


# ── 规则引擎测试 ─────────────────────────────────────────────────

def test_load_rules_returns_empty_list_when_file_empty(tmp_path, monkeypatch):
    import cloud.go2.agent as agent_mod
    rules_file = tmp_path / "rules.json"
    rules_file.write_text("[]")
    monkeypatch.setattr(agent_mod, "RULES_FILE", rules_file)
    assert agent_mod.load_rules() == []


def test_save_and_load_rules_roundtrip(tmp_path, monkeypatch):
    rules_file = tmp_path / "rules.json"
    rules_file.write_text("[]")
    import cloud.go2.agent as agent_mod
    monkeypatch.setattr(agent_mod, "RULES_FILE", rules_file)
    rules = [{"trigger": "人", "action": "Hello", "cooldown_s": 30, "last_triggered": 0}]
    agent_mod.save_rules(rules)
    assert agent_mod.load_rules() == rules


def test_check_rules_triggers_matching_rule(tmp_path, monkeypatch):
    rules_file = tmp_path / "rules.json"
    rule = {"trigger": "人", "action": "Hello", "cooldown_s": 30, "last_triggered": 0}
    rules_file.write_text(json.dumps([rule]))
    import cloud.go2.agent as agent_mod
    monkeypatch.setattr(agent_mod, "RULES_FILE", rules_file)
    triggered = agent_mod.check_rules("画面中有一个人站在桌子旁边")
    assert triggered == ["Hello"]
    updated = agent_mod.load_rules()
    assert updated[0]["last_triggered"] > 0


def test_check_rules_respects_cooldown(tmp_path, monkeypatch):
    rules_file = tmp_path / "rules.json"
    rule = {"trigger": "人", "action": "Hello", "cooldown_s": 30,
            "last_triggered": time.time()}
    rules_file.write_text(json.dumps([rule]))
    import cloud.go2.agent as agent_mod
    monkeypatch.setattr(agent_mod, "RULES_FILE", rules_file)
    triggered = agent_mod.check_rules("画面中有一个人")
    assert triggered == []


def test_check_rules_no_match_returns_empty(tmp_path, monkeypatch):
    rules_file = tmp_path / "rules.json"
    rule = {"trigger": "猫", "action": "Dance1", "cooldown_s": 10, "last_triggered": 0}
    rules_file.write_text(json.dumps([rule]))
    import cloud.go2.agent as agent_mod
    monkeypatch.setattr(agent_mod, "RULES_FILE", rules_file)
    triggered = agent_mod.check_rules("画面中有一张桌子")
    assert triggered == []
