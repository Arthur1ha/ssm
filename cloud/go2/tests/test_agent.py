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
    import cloud.go2.tools as tools_mod
    rules_file = tmp_path / "rules.json"
    rules_file.write_text("[]")
    monkeypatch.setattr(tools_mod, "RULES_FILE", rules_file)
    assert tools_mod.load_rules() == []


def test_save_and_load_rules_roundtrip(tmp_path, monkeypatch):
    rules_file = tmp_path / "rules.json"
    rules_file.write_text("[]")
    import cloud.go2.tools as tools_mod
    monkeypatch.setattr(tools_mod, "RULES_FILE", rules_file)
    rules = [{"trigger": "人", "action": "Hello", "cooldown_s": 30, "last_triggered": 0}]
    tools_mod.save_rules(rules)
    assert tools_mod.load_rules() == rules


def test_check_rules_triggers_matching_rule(tmp_path, monkeypatch):
    rules_file = tmp_path / "rules.json"
    rule = {"trigger": "人", "action": "Hello", "cooldown_s": 30, "last_triggered": 0}
    rules_file.write_text(json.dumps([rule]))
    import cloud.go2.tools as tools_mod
    monkeypatch.setattr(tools_mod, "RULES_FILE", rules_file)
    triggered = tools_mod.check_rules("画面中有一个人站在桌子旁边")
    assert triggered == ["Hello"]
    updated = tools_mod.load_rules()
    assert updated[0]["last_triggered"] > 0


def test_check_rules_respects_cooldown(tmp_path, monkeypatch):
    rules_file = tmp_path / "rules.json"
    rule = {"trigger": "人", "action": "Hello", "cooldown_s": 30,
            "last_triggered": time.time()}
    rules_file.write_text(json.dumps([rule]))
    import cloud.go2.tools as tools_mod
    monkeypatch.setattr(tools_mod, "RULES_FILE", rules_file)
    triggered = tools_mod.check_rules("画面中有一个人")
    assert triggered == []


def test_check_rules_no_match_returns_empty(tmp_path, monkeypatch):
    rules_file = tmp_path / "rules.json"
    rule = {"trigger": "猫", "action": "Dance1", "cooldown_s": 10, "last_triggered": 0}
    rules_file.write_text(json.dumps([rule]))
    import cloud.go2.tools as tools_mod
    monkeypatch.setattr(tools_mod, "RULES_FILE", rules_file)
    triggered = tools_mod.check_rules("画面中有一张桌子")
    assert triggered == []


# ── 工具函数测试 ─────────────────────────────────────────────────

def test_go2_sport_returns_error_when_not_connected():
    import cloud.go2.tools as tools_mod
    import cloud.go2.connection as conn_mod
    conn_mod.go2.is_connected = False
    result = asyncio.run(tools_mod.go2_sport("StandUp"))
    assert "未连接" in result


def test_go2_sport_rejects_unknown_cmd():
    import cloud.go2.tools as tools_mod
    import cloud.go2.connection as conn_mod
    conn_mod.go2.is_connected = True
    result = asyncio.run(tools_mod.go2_sport("FlyToMoon"))
    assert "未知动作" in result
    conn_mod.go2.is_connected = False


def test_go2_sport_sends_command():
    import cloud.go2.tools as tools_mod
    import cloud.go2.connection as conn_mod
    from unittest.mock import AsyncMock, patch
    conn_mod.go2.is_connected = True
    with patch.object(conn_mod.go2, "send_command", new_callable=AsyncMock) as mock_cmd:
        result = asyncio.run(tools_mod.go2_sport("StandUp"))
    mock_cmd.assert_called_once_with("StandUp")
    assert "StandUp" in result
    conn_mod.go2.is_connected = False


def test_go2_move_rejects_unknown_direction():
    import cloud.go2.tools as tools_mod
    import cloud.go2.connection as conn_mod
    conn_mod.go2.is_connected = True
    result = asyncio.run(tools_mod.go2_move("diagonal"))
    assert "未知方向" in result
    conn_mod.go2.is_connected = False


def test_go2_move_sends_move_then_stop():
    import cloud.go2.tools as tools_mod
    import cloud.go2.connection as conn_mod
    from unittest.mock import AsyncMock, patch
    import pytest
    conn_mod.go2.is_connected = True
    calls = []
    async def fake_send(cmd, params=None):
        calls.append((cmd, params))
    with patch.object(conn_mod.go2, "send_command", side_effect=fake_send):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            asyncio.run(tools_mod.go2_move("forward", speed=0.3, duration=1.0))
    assert calls[0][0] == "Move"
    assert calls[0][1]["x"] == pytest.approx(0.3)
    assert calls[1][0] == "StopMove"
    conn_mod.go2.is_connected = False


def test_go2_observe_returns_no_frame_message():
    import cloud.go2.tools as tools_mod
    import cloud.go2.connection as conn_mod
    conn_mod.go2._latest_frame = None
    result = asyncio.run(tools_mod.go2_observe("有没有人？"))
    assert "无可用视频帧" in result


def test_go2_observe_calls_vision_llm(monkeypatch):
    import cloud.go2.tools as tools_mod
    import cloud.go2.connection as conn_mod
    from unittest.mock import AsyncMock, MagicMock
    conn_mod.go2._latest_frame = b"\xff\xd8\xff"
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content="画面中有一张桌子"))
    monkeypatch.setattr(tools_mod, "get_vision_llm", lambda: mock_llm)
    result = asyncio.run(tools_mod.go2_observe("描述场景"))
    assert result == "画面中有一张桌子"
    conn_mod.go2._latest_frame = None


def test_go2_status_disconnected():
    import cloud.go2.tools as tools_mod
    import cloud.go2.connection as conn_mod
    conn_mod.go2.is_connected = False
    result = tools_mod.go2_status()
    assert "未连接" in result


def test_go2_status_connected():
    import cloud.go2.tools as tools_mod
    import cloud.go2.connection as conn_mod
    conn_mod.go2.is_connected = True
    conn_mod.go2._robot_state = {"mode": 1, "body_height": 0.32, "velocity": [0, 0, 0]}
    result = tools_mod.go2_status()
    assert "已连接" in result
    conn_mod.go2.is_connected = False


def test_go2_add_rule_rejects_invalid_action(tmp_path, monkeypatch):
    import cloud.go2.tools as tools_mod
    monkeypatch.setattr(tools_mod, "RULES_FILE", tmp_path / "rules.json")
    (tmp_path / "rules.json").write_text("[]")
    result = tools_mod.go2_add_rule("人", "FlyAway")
    assert "不支持" in result


def test_go2_add_rule_writes_to_file(tmp_path, monkeypatch):
    import cloud.go2.tools as tools_mod
    rules_file = tmp_path / "rules.json"
    rules_file.write_text("[]")
    monkeypatch.setattr(tools_mod, "RULES_FILE", rules_file)
    result = tools_mod.go2_add_rule("人", "Hello", cooldown_s=30)
    assert "已添加规则" in result
    saved = json.loads(rules_file.read_text())
    assert saved[0]["trigger"] == "人"
    assert saved[0]["action"] == "Hello"


def test_go2_add_rule_deduplicates(tmp_path, monkeypatch):
    import cloud.go2.tools as tools_mod
    rules_file = tmp_path / "rules.json"
    existing = [{"trigger": "人", "action": "Hello", "cooldown_s": 30, "last_triggered": 0}]
    rules_file.write_text(json.dumps(existing))
    monkeypatch.setattr(tools_mod, "RULES_FILE", rules_file)
    tools_mod.go2_add_rule("人", "Hello", cooldown_s=60)
    saved = json.loads(rules_file.read_text())
    assert len(saved) == 1
    assert saved[0]["cooldown_s"] == 60


def test_go2_list_rules_empty(tmp_path, monkeypatch):
    import cloud.go2.tools as tools_mod
    rules_file = tmp_path / "rules.json"
    rules_file.write_text("[]")
    monkeypatch.setattr(tools_mod, "RULES_FILE", rules_file)
    assert "没有规则" in tools_mod.go2_list_rules()


def test_go2_list_rules_shows_rules(tmp_path, monkeypatch):
    import cloud.go2.tools as tools_mod
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps([
        {"trigger": "人", "action": "Hello", "cooldown_s": 30, "last_triggered": 0}
    ]))
    monkeypatch.setattr(tools_mod, "RULES_FILE", rules_file)
    result = tools_mod.go2_list_rules()
    assert "人" in result
    assert "Hello" in result


# ── LangGraph 流水线测试 ──────────────────────────────────────────

def test_run_agent_returns_early_when_disconnected():
    import cloud.go2.agent as agent_mod
    import cloud.go2.connection as conn_mod
    conn_mod.go2.is_connected = False
    result = asyncio.run(agent_mod.run_agent("s1", "站起来"))
    assert "未连接" in result["response"]
    assert result["actions_taken"] == []


def test_run_agent_executes_sport_command(monkeypatch):
    import cloud.go2.agent as agent_mod
    import cloud.go2.connection as conn_mod
    from unittest.mock import AsyncMock, MagicMock, patch

    conn_mod.go2.is_connected = True

    async def fake_planner(state):
        return {**state, "planned_tools": [{"tool": "go2_sport", "params": {"cmd": "Hello"}}],
                "early_exit": False}

    mock_text_llm = MagicMock()
    mock_text_llm.ainvoke = AsyncMock(return_value=MagicMock(content="好的，Go2 已挥手问好"))
    monkeypatch.setattr(agent_mod, "get_text_llm", lambda: mock_text_llm)

    with patch.object(conn_mod.go2, "send_command", new_callable=AsyncMock):
        with patch.object(agent_mod, "planner_node", side_effect=fake_planner):
            result = asyncio.run(agent_mod.run_agent("s1", "挥手"))

    assert "actions_taken" in result
    assert "go2_sport" in result["actions_taken"]
    conn_mod.go2.is_connected = False



# ── /api/go2/chat 路由测试 ────────────────────────────────────────

from fastapi import FastAPI
from fastapi.testclient import TestClient
from cloud.go2.router import router as go2_router


@pytest.fixture
def chat_client():
    app = FastAPI()
    app.include_router(go2_router)
    return TestClient(app)


def test_chat_returns_response(chat_client, monkeypatch):
    import cloud.go2.agent as agent_mod
    async def fake_run_agent(session_id, message):
        return {"response": "好的，已站起来", "actions_taken": ["go2_sport"]}
    monkeypatch.setattr(agent_mod, "run_agent", fake_run_agent)
    r = chat_client.post("/api/go2/chat", json={"session_id": "test", "message": "站起来"})
    assert r.status_code == 200
    data = r.json()
    assert data["response"] == "好的，已站起来"
    assert data["actions_taken"] == ["go2_sport"]


def test_chat_missing_message_returns_422(chat_client):
    r = chat_client.post("/api/go2/chat", json={"session_id": "test"})
    assert r.status_code == 422


def test_chat_default_session_id(chat_client, monkeypatch):
    import cloud.go2.agent as agent_mod
    received = {}
    async def fake_run_agent(session_id, message):
        received["session_id"] = session_id
        return {"response": "ok", "actions_taken": []}
    monkeypatch.setattr(agent_mod, "run_agent", fake_run_agent)
    chat_client.post("/api/go2/chat", json={"message": "你好"})
    assert received["session_id"] == "default"


def test_new_tools_in_fn_map():
    from cloud.go2.tools import TOOL_FN_MAP, TOOL_DESCRIPTIONS
    for name in ("go2_tag_location", "go2_navigate_to", "go2_list_locations",
                 "go2_set_led"):
        assert name in TOOL_FN_MAP, f"{name} missing from TOOL_FN_MAP"
        assert name in TOOL_DESCRIPTIONS, f"{name} missing from TOOL_DESCRIPTIONS"


# ── ReactiveMind 性格与记忆集成测试 ──────────────────────────────────

def test_autonomous_reason_uses_system_message(monkeypatch):
    import cloud.go2.reactive_mind as rm_mod
    from cloud.go2.episode_memory import EpisodeMemory
    from langchain_core.messages import SystemMessage

    fresh_memory = EpisodeMemory()
    monkeypatch.setattr(rm_mod, "episode_memory", fresh_memory)

    captured = []

    async def fake_ainvoke(messages):
        captured.extend(messages)
        return MagicMock(content='{"action": null, "reason": "平静"}')

    mock_llm = MagicMock()
    mock_llm.ainvoke = fake_ainvoke
    monkeypatch.setattr(rm_mod, "get_text_llm", lambda: mock_llm)

    mind = rm_mod.ReactiveMind()
    frame = {
        "ts": time.time(), "persons": {"detected": False, "count": 0},
        "faces": {"detected": False, "count": 0},
        "changed": True, "change_type": "none",
    }
    asyncio.run(mind._autonomous_reason(frame))

    assert any(isinstance(m, SystemMessage) for m in captured)


def test_autonomous_reason_records_action_taken_in_memory(monkeypatch):
    import cloud.go2.reactive_mind as rm_mod
    import cloud.go2.connection as conn_mod
    from cloud.go2.episode_memory import EpisodeMemory

    fresh_memory = EpisodeMemory()
    monkeypatch.setattr(rm_mod, "episode_memory", fresh_memory)

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=MagicMock(content='{"action": "Hello", "reason": "有人进入"}')
    )
    monkeypatch.setattr(rm_mod, "get_text_llm", lambda: mock_llm)

    conn_mod.go2.is_connected = True
    with patch.object(conn_mod.go2, "send_command", new_callable=AsyncMock):
        mind = rm_mod.ReactiveMind()
        frame = {
            "ts": time.time(), "persons": {"detected": True, "count": 1},
            "faces": {"detected": False, "count": 0},
            "changed": True, "change_type": "person_entered",
        }
        asyncio.run(mind._autonomous_reason(frame))
    conn_mod.go2.is_connected = False

    assert any("Hello" in e["content"] for e in fresh_memory.entries())


def test_run_agent_records_user_command_in_memory(monkeypatch):
    import cloud.go2.agent as agent_mod
    import cloud.go2.connection as conn_mod
    from cloud.go2.episode_memory import EpisodeMemory
    import cloud.go2.episode_memory as mem_mod

    fresh_memory = EpisodeMemory()
    monkeypatch.setattr(mem_mod, "episode_memory", fresh_memory)
    monkeypatch.setattr(agent_mod, "episode_memory", fresh_memory)

    conn_mod.go2.is_connected = False
    asyncio.run(agent_mod.run_agent("s1", "测试指令"))

    assert any("测试指令" in e["content"] for e in fresh_memory.entries())


# ── ReactiveMind 触发闸门测试 ────────────────────────────────────────

def _vision_frame(persons=0, faces=0, changed=False, change_type="none"):
    return {
        "ts": time.time(),
        "persons": {"detected": persons > 0, "count": persons},
        "faces": {"detected": faces > 0, "count": faces},
        "changed": changed, "change_type": change_type,
    }


async def _drain_tasks():
    """等待 on_vision_frame 通过 create_task 派发的 fire-and-forget 协程全部完成。"""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


def test_on_vision_frame_triggers_on_face_change(monkeypatch):
    """人数不变、只有人脸出现时，也应触发自主推理。"""
    from cloud.go2.agentcore.skills import reactive as rm_mod
    monkeypatch.setattr(rm_mod, "check_rules", lambda obs: [])   # 隔离规则层
    mind = rm_mod.ReactiveMind()

    called = []
    async def fake_reason(frame):
        called.append(frame)
    monkeypatch.setattr(mind, "_autonomous_reason", fake_reason)

    async def run():
        mind.on_vision_frame(_vision_frame(persons=0, faces=0))   # 基线
        mind.on_vision_frame(_vision_frame(persons=0, faces=1))   # 露脸
        await _drain_tasks()
    asyncio.run(run())

    assert len(called) == 1


def test_on_vision_frame_triggers_on_person_change(monkeypatch):
    """人数跨帧变化仍应触发（保持原有行为）。"""
    from cloud.go2.agentcore.skills import reactive as rm_mod
    monkeypatch.setattr(rm_mod, "check_rules", lambda obs: [])   # 隔离规则层
    mind = rm_mod.ReactiveMind()

    called = []
    async def fake_reason(frame):
        called.append(frame)
    monkeypatch.setattr(mind, "_autonomous_reason", fake_reason)

    async def run():
        mind.on_vision_frame(_vision_frame(persons=0, faces=0))
        mind.on_vision_frame(_vision_frame(persons=1, faces=0))
        await _drain_tasks()
    asyncio.run(run())

    assert len(called) == 1


def test_on_vision_frame_no_trigger_when_steady(monkeypatch):
    """画面无变化时不应触发。"""
    from cloud.go2.agentcore.skills import reactive as rm_mod
    monkeypatch.setattr(rm_mod, "check_rules", lambda obs: [])   # 隔离规则层
    mind = rm_mod.ReactiveMind()

    called = []
    async def fake_reason(frame):
        called.append(frame)
    monkeypatch.setattr(mind, "_autonomous_reason", fake_reason)

    async def run():
        mind.on_vision_frame(_vision_frame(persons=1, faces=1))
        mind.on_vision_frame(_vision_frame(persons=1, faces=1))
        await _drain_tasks()
    asyncio.run(run())

    assert len(called) == 0


def test_on_vision_frame_executes_rule_action(monkeypatch):
    """规则命中时应真正下发动作，而不只是写冷却。"""
    from cloud.go2.agentcore.skills import reactive as rm_mod
    monkeypatch.setattr(rm_mod, "check_rules", lambda obs: ["Hello"])
    dispatched = []
    async def fake_sport(cmd):
        dispatched.append(cmd)
    monkeypatch.setattr(rm_mod, "go2_sport", fake_sport)
    mind = rm_mod.ReactiveMind()

    async def run():
        mind.on_vision_frame(_vision_frame(persons=1, faces=0))
        await _drain_tasks()
    asyncio.run(run())

    assert dispatched == ["Hello"]


def test_rule_match_takes_priority_over_autonomous(monkeypatch):
    """规则命中时跳过 LLM 自主推理，避免双重动作。"""
    from cloud.go2.agentcore.skills import reactive as rm_mod
    monkeypatch.setattr(rm_mod, "check_rules", lambda obs: ["Hello"])
    async def fake_sport(cmd):
        pass
    monkeypatch.setattr(rm_mod, "go2_sport", fake_sport)
    mind = rm_mod.ReactiveMind()

    reasoned = []
    async def fake_reason(frame):
        reasoned.append(frame)
    monkeypatch.setattr(mind, "_autonomous_reason", fake_reason)

    async def run():
        mind.on_vision_frame(_vision_frame(persons=0, faces=0))   # 基线
        mind.on_vision_frame(_vision_frame(persons=1, faces=1))   # 画面变化 + 规则命中
        await _drain_tasks()
    asyncio.run(run())

    assert reasoned == []   # 规则优先，未走自主推理


def test_go2_add_rule_strips_tool_prefix(tmp_path, monkeypatch):
    """LLM 常把工具名混进 action（"go2_sport Hello"），应剥掉前缀只存动作名。"""
    from cloud.go2.agentcore.tools import tools as tools_mod
    rules_file = tmp_path / "rules.json"
    rules_file.write_text("[]")
    monkeypatch.setattr(tools_mod, "RULES_FILE", rules_file)
    result = tools_mod.go2_add_rule("人", "go2_sport Hello", cooldown_s=30)
    assert "已添加规则" in result
    saved = json.loads(rules_file.read_text())
    assert saved[0]["action"] == "Hello"


def test_go2_add_rule_normalizes_case(tmp_path, monkeypatch):
    """大小写不敏感，归一到规范动作名。"""
    from cloud.go2.agentcore.tools import tools as tools_mod
    rules_file = tmp_path / "rules.json"
    rules_file.write_text("[]")
    monkeypatch.setattr(tools_mod, "RULES_FILE", rules_file)
    tools_mod.go2_add_rule("人", "hello")
    saved = json.loads(rules_file.read_text())
    assert saved[0]["action"] == "Hello"


def test_go2_add_rule_rejects_truly_unknown(tmp_path, monkeypatch):
    from cloud.go2.agentcore.tools import tools as tools_mod
    rules_file = tmp_path / "rules.json"
    rules_file.write_text("[]")
    monkeypatch.setattr(tools_mod, "RULES_FILE", rules_file)
    result = tools_mod.go2_add_rule("人", "FlyToMoon")
    assert "不支持" in result


def test_dispatch_action_respects_cooldown(monkeypatch):
    """同一动作处于冷却中时不重复下发（规则层与自主层共用冷却）。"""
    from cloud.go2.agentcore.skills import reactive as rm_mod
    monkeypatch.setattr(rm_mod, "check_rules", lambda obs: ["Hello"])
    dispatched = []
    async def fake_sport(cmd):
        dispatched.append(cmd)
    monkeypatch.setattr(rm_mod, "go2_sport", fake_sport)
    mind = rm_mod.ReactiveMind()
    mind._action_cooldowns["Hello"] = time.time()   # 刚执行过，仍在冷却

    async def run():
        mind.on_vision_frame(_vision_frame(persons=1, faces=0))
        await _drain_tasks()
    asyncio.run(run())

    assert dispatched == []


def test_executor_uses_system_message_for_response(monkeypatch):
    import cloud.go2.agent as agent_mod
    import cloud.go2.connection as conn_mod
    from langchain_core.messages import SystemMessage

    conn_mod.go2.is_connected = True

    captured = []

    async def fake_ainvoke(messages):
        captured.extend(messages)
        return MagicMock(content="好的，已执行")

    mock_llm = MagicMock()
    mock_llm.ainvoke = fake_ainvoke
    monkeypatch.setattr(agent_mod, "get_text_llm", lambda: mock_llm)

    async def fake_planner(state):
        return {**state, "planned_tools": [], "early_exit": False}

    with patch.object(agent_mod, "planner_node", side_effect=fake_planner):
        asyncio.run(agent_mod.run_agent("s1", "你好"))

    assert any(isinstance(m, SystemMessage) for m in captured)
    conn_mod.go2.is_connected = False


def test_run_agent_publishes_thought(monkeypatch):
    import cloud.go2.agent as agent_mod
    import cloud.go2.connection as conn_mod
    from unittest.mock import AsyncMock, MagicMock, patch

    conn_mod.go2.is_connected = True

    async def fake_planner(state):
        return {**state, "planned_tools": [], "early_exit": False}

    mock_text_llm = MagicMock()
    mock_text_llm.ainvoke = AsyncMock(return_value=MagicMock(content="汪！我这就去迎接你！"))
    monkeypatch.setattr(agent_mod, "get_text_llm", lambda: mock_text_llm)

    published = []
    mock_client = MagicMock()
    mock_client.publish = lambda topic, payload: published.append((topic, payload))
    monkeypatch.setattr(agent_mod, "_get_mqtt_client", lambda: mock_client)

    with patch.object(agent_mod, "planner_node", side_effect=fake_planner):
        result = asyncio.run(agent_mod.run_agent("s1", "来迎接我"))

    thought_publishes = [p for p in published if p[0] == "ssm/agents/go2/thought"]
    assert len(thought_publishes) == 1
    payload = json.loads(thought_publishes[0][1])
    assert payload["type"] == "think"
    assert payload["text"] == "汪！我这就去迎接你！"
    conn_mod.go2.is_connected = False
