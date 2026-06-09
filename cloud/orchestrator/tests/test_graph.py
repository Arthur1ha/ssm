"""cloud.orchestrator.tests.test_graph — card-driven Planner/Dispatcher 单元测试。

覆盖：
- Planner：基于 card 集合输出合法 {slug, skill_id, params}，过滤非法项。
- Dispatcher：mqtt transport 走 do_publish_task，http transport 走线程池非阻塞。
- Evaluator：http 结果已在 task_results，跳过 MQTT 轮询；只对 mqtt task 轮询。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import graph as graph_mod   # noqa: E402
import tools as _t          # noqa: E402


# ── card fixtures ────────────────────────────────────────────────

LED_CARD = {
    "slug": "esp32_desk_led",
    "name": "桌面灯",
    "description": "WS2812 灯环",
    "agent_type": "actuator",
    "online": True,
    "transport": {"kind": "mqtt", "task_topic": "ssm/task/esp32_desk_led/{task_id}"},
    "skills": [
        {
            "id": "set_light_state",
            "name": "开关灯",
            "tags": ["actuator", "light"],
            "params_schema": {"type": "object", "properties": {"state": {"enum": ["BRIGHT", "DIM", "OFF"]}}},
            "invoke": {"action": "SET_STATE"},
        },
        {
            "id": "set_light_color",
            "name": "设置灯光颜色",
            "tags": ["actuator", "light"],
            "params_schema": {"type": "object", "properties": {}},
            "invoke": {"action": "SET_COLOR"},
        },
    ],
    "state": {},
}

GO2_CARD = {
    "slug": "go2",
    "name": "Go2 机器狗",
    "description": "四足机器人",
    "agent_type": "robot",
    "online": True,
    "transport": {"kind": "http", "endpoint": "/api/go2/chat"},
    "skills": [
        {
            "id": "go2_navigate",
            "name": "导航到命名地点",
            "tags": ["navigation", "robot"],
            "params_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
            "invoke": {"action": "NAVIGATE"},
        },
        {
            "id": "go2_sport",
            "name": "预定义动作",
            "tags": ["motion", "robot"],
            "params_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            "invoke": {"action": "SPORT"},
        },
    ],
    "state": {},
}


class FakeRegistry:
    """最小 CardRegistry 替身，返回固定 card 集合。"""

    def __init__(self, cards):
        self._cards = {c["slug"]: c for c in cards}

    def get_all_cards(self):
        return dict(self._cards)

    def get_card(self, slug):
        c = self._cards.get(slug)
        return dict(c) if c else None


@pytest.fixture(autouse=True)
def _reset_tools(monkeypatch):
    """每个测试隔离 tools 模块的全局依赖。"""
    monkeypatch.setattr(_t, "_state", MagicMock(), raising=False)
    monkeypatch.setattr(_t, "_mqtt", MagicMock(), raising=False)
    monkeypatch.setattr(_t, "_registry", None, raising=False)
    yield


# ── Planner 测试 ─────────────────────────────────────────────────

def _stub_llm(content):
    """构造一个返回固定 content 的 LLM 替身。"""
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content=content)
    return llm


def test_planner_builds_valid_tasks(monkeypatch):
    """Planner 输出合法的 {slug, skill_id, task_id, params}。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([LED_CARD, GO2_CARD]))
    monkeypatch.setattr(_t, "do_publish_feedback", MagicMock())

    llm = _stub_llm(
        '[{"slug": "esp32_desk_led", "skill_id": "set_light_state", "params": {"state": "BRIGHT"}}]'
    )
    node = graph_mod._make_planner_node(llm)
    out = node({"session_id": "s1", "user_msg": "开灯", "requirements": [],
                "planned_tasks": [], "task_results": {}, "response_text": "", "early_exit": False})

    assert out["early_exit"] is False
    assert len(out["planned_tasks"]) == 1
    t = out["planned_tasks"][0]
    assert t["slug"] == "esp32_desk_led"
    assert t["skill_id"] == "set_light_state"
    assert t["task_id"] == "s1_t0"
    assert t["params"] == {"state": "BRIGHT"}


def test_planner_filters_invalid_slug_and_skill(monkeypatch):
    """非法 slug 或 skill_id 的项被过滤。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([LED_CARD, GO2_CARD]))
    monkeypatch.setattr(_t, "do_publish_feedback", MagicMock())

    llm = _stub_llm(
        '['
        '{"slug": "nonexistent", "skill_id": "set_light_state", "params": {}},'   # bad slug
        '{"slug": "esp32_desk_led", "skill_id": "bogus_skill", "params": {}},'    # bad skill
        '{"slug": "go2", "skill_id": "go2_navigate", "params": {"name": "厨房"}}'  # valid
        ']'
    )
    node = graph_mod._make_planner_node(llm)
    out = node({"session_id": "s2", "user_msg": "去厨房", "requirements": [],
                "planned_tasks": [], "task_results": {}, "response_text": "", "early_exit": False})

    assert len(out["planned_tasks"]) == 1
    assert out["planned_tasks"][0]["slug"] == "go2"
    assert out["planned_tasks"][0]["skill_id"] == "go2_navigate"


def test_planner_early_exit_when_no_cards(monkeypatch):
    """注册表为空时 early_exit 并发出 failed 反馈。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([]))
    fb = MagicMock()
    monkeypatch.setattr(_t, "do_publish_feedback", fb)

    llm = _stub_llm("[]")
    node = graph_mod._make_planner_node(llm)
    out = node({"session_id": "s3", "user_msg": "开灯", "requirements": [],
                "planned_tasks": [], "task_results": {}, "response_text": "", "early_exit": False})

    assert out["early_exit"] is True
    assert out["planned_tasks"] == []
    assert any(call.args[1] == "failed" for call in fb.call_args_list)


# ── Dispatcher 测试 ──────────────────────────────────────────────

def test_dispatcher_mqtt_uses_publish_task(monkeypatch):
    """mqtt transport 调用 do_publish_task，action 取自 skill.invoke.action。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([LED_CARD]))
    monkeypatch.setattr(_t, "do_publish_feedback", MagicMock())
    pub = MagicMock()
    monkeypatch.setattr(_t, "do_publish_task", pub)

    node = graph_mod._make_dispatcher_node()
    state = {
        "session_id": "s1",
        "planned_tasks": [
            {"slug": "esp32_desk_led", "skill_id": "set_light_state",
             "task_id": "s1_t0", "params": {"state": "BRIGHT"}}
        ],
        "task_results": {}, "early_exit": False,
    }
    out = node(state)

    pub.assert_called_once()
    args = pub.call_args.args
    assert args[0] == "esp32_desk_led"   # slug
    assert args[1] == "s1_t0"            # task_id
    assert args[2] == "SET_STATE"        # action from invoke
    assert args[3] == {"state": "BRIGHT"}
    # mqtt task 不在 dispatcher 阶段填结果（留给 Evaluator 轮询）
    assert "s1_t0" not in out["task_results"]


def test_dispatcher_http_uses_thread_pool(monkeypatch):
    """http transport 调用 do_http_dispatch 并把结果直接填入 task_results。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([GO2_CARD]))
    monkeypatch.setattr(_t, "do_publish_feedback", MagicMock())
    http = MagicMock(return_value={"result": "ok", "message": "已到达"})
    monkeypatch.setattr(_t, "do_http_dispatch", http)

    node = graph_mod._make_dispatcher_node()
    state = {
        "session_id": "sess",
        "planned_tasks": [
            {"slug": "go2", "skill_id": "go2_navigate",
             "task_id": "sess_t0", "params": {"name": "厨房"}}
        ],
        "task_results": {}, "early_exit": False,
    }
    out = node(state)

    http.assert_called_once()
    endpoint, body, timeout = http.call_args.args
    assert endpoint == "/api/go2/chat"
    assert body["session_id"] == "sess"
    assert body["skill_id"] == "go2_navigate"
    assert body["params"] == {"name": "厨房"}
    assert "导航到命名地点" in body["message"]
    # navigation tag → 30s 超时
    assert timeout == 30
    # http 结果直接进 task_results
    assert out["task_results"]["sess_t0"] == {"result": "ok", "message": "已到达"}


def test_dispatcher_http_timeout_recorded(monkeypatch):
    """http 调用异常（超时）记为 {"result": "timeout"}。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([GO2_CARD]))
    monkeypatch.setattr(_t, "do_publish_feedback", MagicMock())

    def _boom(*a, **k):
        raise TimeoutError("boom")

    monkeypatch.setattr(_t, "do_http_dispatch", _boom)

    node = graph_mod._make_dispatcher_node()
    state = {
        "session_id": "sess",
        "planned_tasks": [
            {"slug": "go2", "skill_id": "go2_sport",
             "task_id": "sess_t0", "params": {"cmd": "Hello"}}
        ],
        "task_results": {}, "early_exit": False,
    }
    out = node(state)
    assert out["task_results"]["sess_t0"]["result"] == "timeout"


def test_dispatcher_non_navigation_timeout_is_10s(monkeypatch):
    """非 navigation skill 超时为 10s。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([GO2_CARD]))
    monkeypatch.setattr(_t, "do_publish_feedback", MagicMock())
    http = MagicMock(return_value={"result": "ok"})
    monkeypatch.setattr(_t, "do_http_dispatch", http)

    node = graph_mod._make_dispatcher_node()
    state = {
        "session_id": "sess",
        "planned_tasks": [
            {"slug": "go2", "skill_id": "go2_sport",
             "task_id": "sess_t0", "params": {"cmd": "Hello"}}
        ],
        "task_results": {}, "early_exit": False,
    }
    node(state)
    _, _, timeout = http.call_args.args
    assert timeout == 10


# ── Evaluator 测试 ───────────────────────────────────────────────

def test_evaluator_skips_polling_for_http_results(monkeypatch):
    """http 结果已在 task_results，Evaluator 不对其轮询 MQTT。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([GO2_CARD, LED_CARD]))
    state_obj = MagicMock()
    state_obj.get_task_result.return_value = {"result": "ok", "task_id": "s_t1"}
    monkeypatch.setattr(_t, "_state", state_obj)

    node = graph_mod._make_evaluator_node()
    state = {
        "early_exit": False,
        "planned_tasks": [
            {"slug": "go2", "skill_id": "go2_sport", "task_id": "s_t0", "params": {}},
            {"slug": "esp32_desk_led", "skill_id": "set_light_state", "task_id": "s_t1", "params": {}},
        ],
        "task_results": {"s_t0": {"result": "ok", "task_id": "s_t0"}},
    }
    out = node(state)

    # http task 结果保持不变，未被轮询覆盖
    assert out["task_results"]["s_t0"]["result"] == "ok"
    # mqtt task 通过轮询拿到结果
    assert out["task_results"]["s_t1"]["result"] == "ok"
    # 只对 mqtt task 查询过 state
    state_obj.get_task_result.assert_called_with("s_t1")
