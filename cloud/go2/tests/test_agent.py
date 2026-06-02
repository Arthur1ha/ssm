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


# ── 工具函数测试 ─────────────────────────────────────────────────

def test_go2_sport_returns_error_when_not_connected():
    import cloud.go2.agent as agent_mod
    import cloud.go2.connection as conn_mod
    conn_mod.go2.is_connected = False
    result = asyncio.run(agent_mod.go2_sport("StandUp"))
    assert "未连接" in result


def test_go2_sport_rejects_unknown_cmd():
    import cloud.go2.agent as agent_mod
    import cloud.go2.connection as conn_mod
    conn_mod.go2.is_connected = True
    result = asyncio.run(agent_mod.go2_sport("FlyToMoon"))
    assert "未知动作" in result
    conn_mod.go2.is_connected = False


def test_go2_sport_sends_command():
    import cloud.go2.agent as agent_mod
    import cloud.go2.connection as conn_mod
    from unittest.mock import AsyncMock, patch
    conn_mod.go2.is_connected = True
    with patch.object(conn_mod.go2, "send_command", new_callable=AsyncMock) as mock_cmd:
        result = asyncio.run(agent_mod.go2_sport("StandUp"))
    mock_cmd.assert_called_once_with("StandUp")
    assert "StandUp" in result
    conn_mod.go2.is_connected = False


def test_go2_move_rejects_unknown_direction():
    import cloud.go2.agent as agent_mod
    import cloud.go2.connection as conn_mod
    conn_mod.go2.is_connected = True
    result = asyncio.run(agent_mod.go2_move("diagonal"))
    assert "未知方向" in result
    conn_mod.go2.is_connected = False


def test_go2_move_sends_move_then_stop():
    import cloud.go2.agent as agent_mod
    import cloud.go2.connection as conn_mod
    from unittest.mock import AsyncMock, patch
    import pytest
    conn_mod.go2.is_connected = True
    calls = []
    async def fake_send(cmd, params=None):
        calls.append((cmd, params))
    with patch.object(conn_mod.go2, "send_command", side_effect=fake_send):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            asyncio.run(agent_mod.go2_move("forward", speed=0.3, duration=1.0))
    assert calls[0][0] == "Move"
    assert calls[0][1]["x"] == pytest.approx(0.3)
    assert calls[1][0] == "StopMove"
    conn_mod.go2.is_connected = False


def test_go2_observe_returns_no_frame_message():
    import cloud.go2.agent as agent_mod
    import cloud.go2.connection as conn_mod
    conn_mod.go2._latest_frame = None
    result = asyncio.run(agent_mod.go2_observe("有没有人？"))
    assert "无可用视频帧" in result


def test_go2_observe_calls_vision_llm(monkeypatch):
    import cloud.go2.agent as agent_mod
    import cloud.go2.connection as conn_mod
    from unittest.mock import AsyncMock, MagicMock
    conn_mod.go2._latest_frame = b"\xff\xd8\xff"
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content="画面中有一张桌子"))
    monkeypatch.setattr(agent_mod, "get_vision_llm", lambda: mock_llm)
    result = asyncio.run(agent_mod.go2_observe("描述场景"))
    assert result == "画面中有一张桌子"
    conn_mod.go2._latest_frame = None


def test_go2_status_disconnected():
    import cloud.go2.agent as agent_mod
    import cloud.go2.connection as conn_mod
    conn_mod.go2.is_connected = False
    result = agent_mod.go2_status()
    assert "未连接" in result


def test_go2_status_connected():
    import cloud.go2.agent as agent_mod
    import cloud.go2.connection as conn_mod
    conn_mod.go2.is_connected = True
    conn_mod.go2._robot_state = {"mode": 1, "body_height": 0.32, "velocity": [0, 0, 0]}
    result = agent_mod.go2_status()
    assert "已连接" in result
    conn_mod.go2.is_connected = False


def test_go2_add_rule_rejects_invalid_action(tmp_path, monkeypatch):
    import cloud.go2.agent as agent_mod
    monkeypatch.setattr(agent_mod, "RULES_FILE", tmp_path / "rules.json")
    (tmp_path / "rules.json").write_text("[]")
    result = agent_mod.go2_add_rule("人", "FlyAway")
    assert "不支持" in result


def test_go2_add_rule_writes_to_file(tmp_path, monkeypatch):
    import cloud.go2.agent as agent_mod
    rules_file = tmp_path / "rules.json"
    rules_file.write_text("[]")
    monkeypatch.setattr(agent_mod, "RULES_FILE", rules_file)
    result = agent_mod.go2_add_rule("人", "Hello", cooldown_s=30)
    assert "已添加规则" in result
    saved = json.loads(rules_file.read_text())
    assert saved[0]["trigger"] == "人"
    assert saved[0]["action"] == "Hello"


def test_go2_add_rule_deduplicates(tmp_path, monkeypatch):
    import cloud.go2.agent as agent_mod
    rules_file = tmp_path / "rules.json"
    existing = [{"trigger": "人", "action": "Hello", "cooldown_s": 30, "last_triggered": 0}]
    rules_file.write_text(json.dumps(existing))
    monkeypatch.setattr(agent_mod, "RULES_FILE", rules_file)
    agent_mod.go2_add_rule("人", "Hello", cooldown_s=60)
    saved = json.loads(rules_file.read_text())
    assert len(saved) == 1
    assert saved[0]["cooldown_s"] == 60


def test_go2_list_rules_empty(tmp_path, monkeypatch):
    import cloud.go2.agent as agent_mod
    rules_file = tmp_path / "rules.json"
    rules_file.write_text("[]")
    monkeypatch.setattr(agent_mod, "RULES_FILE", rules_file)
    assert "没有规则" in agent_mod.go2_list_rules()


def test_go2_list_rules_shows_rules(tmp_path, monkeypatch):
    import cloud.go2.agent as agent_mod
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps([
        {"trigger": "人", "action": "Hello", "cooldown_s": 30, "last_triggered": 0}
    ]))
    monkeypatch.setattr(agent_mod, "RULES_FILE", rules_file)
    result = agent_mod.go2_list_rules()
    assert "人" in result
    assert "Hello" in result
