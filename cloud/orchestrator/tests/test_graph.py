"""cloud.orchestrator.tests.test_graph — card-driven Planner/Dispatcher 单元测试。

覆盖：
- Planner：基于 card 集合输出合法 {slug, skill_id, params}，过滤非法项。
- Dispatcher：mqtt transport 走 do_publish_task，http transport 走线程池非阻塞。
- Evaluator：http 结果已在 task_results，跳过 MQTT 轮询；只对 mqtt task 轮询。
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import graph as graph_mod   # noqa: E402
import tools as _t          # noqa: E402


# ── card fixtures ────────────────────────────────────────────────

LED_CARD = {
    "unit_id": "esp32_desk_led",
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
    "unit_id": "go2",
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
        self._cards = {c["unit_id"]: c for c in cards}

    def get_all_cards(self):
        return dict(self._cards)

    def get_card(self, unit_id):
        c = self._cards.get(unit_id)
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


def _planner_state(session_id, user_msg):
    """构造 Planner 输入 state（含 Task 4 新增字段）。"""
    return {"session_id": session_id, "user_msg": user_msg, "requirements": [],
            "route": "", "planned_tasks": [], "rule": {},
            "task_results": {}, "response_text": "", "early_exit": False}


def test_planner_builds_valid_tasks(monkeypatch):
    """route=act 时输出合法的 {slug, skill_id, task_id, params}。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([LED_CARD, GO2_CARD]))
    monkeypatch.setattr(_t, "do_publish_feedback", MagicMock())

    llm = _stub_llm(
        '{"route": "act", "tasks": '
        '[{"unit_id": "esp32_desk_led", "skill_id": "set_light_state", "params": {"state": "BRIGHT"}}]}'
    )
    node = graph_mod._make_planner_node(llm)
    out = node(_planner_state("s1", "开灯"))

    assert out["route"] == "act"
    assert out["early_exit"] is False
    assert len(out["planned_tasks"]) == 1
    t = out["planned_tasks"][0]
    assert t["unit_id"] == "esp32_desk_led"
    assert t["skill_id"] == "set_light_state"
    assert t["task_id"] == "s1_t0"
    assert t["params"] == {"state": "BRIGHT"}


def test_planner_filters_invalid_slug_and_skill(monkeypatch):
    """非法 slug 或 skill_id 的项被过滤。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([LED_CARD, GO2_CARD]))
    monkeypatch.setattr(_t, "do_publish_feedback", MagicMock())

    llm = _stub_llm(
        '{"route": "act", "tasks": ['
        '{"unit_id": "nonexistent", "skill_id": "set_light_state", "params": {}},'   # bad slug
        '{"unit_id": "esp32_desk_led", "skill_id": "bogus_skill", "params": {}},'    # bad skill
        '{"unit_id": "go2", "skill_id": "go2_navigate", "params": {"name": "厨房"}}'  # valid
        ']}'
    )
    node = graph_mod._make_planner_node(llm)
    out = node(_planner_state("s2", "去厨房"))

    assert out["route"] == "act"
    assert len(out["planned_tasks"]) == 1
    assert out["planned_tasks"][0]["unit_id"] == "go2"
    assert out["planned_tasks"][0]["skill_id"] == "go2_navigate"


def test_planner_early_exit_when_no_cards(monkeypatch):
    """注册表为空时 early_exit 并发出 failed 反馈。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([]))
    fb = MagicMock()
    monkeypatch.setattr(_t, "do_publish_feedback", fb)

    llm = _stub_llm('{"route": "act", "tasks": []}')
    node = graph_mod._make_planner_node(llm)
    out = node(_planner_state("s3", "开灯"))

    assert out["early_exit"] is True
    assert out["planned_tasks"] == []
    assert any(call.args[1] == "failed" for call in fb.call_args_list)


# ── Planner 分类（route）测试 ─────────────────────────────────────

def test_planner_route_chat(monkeypatch):
    """route=chat 时 response_text 填入 answer，无 tasks。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([LED_CARD, GO2_CARD]))
    monkeypatch.setattr(_t, "do_publish_feedback", MagicMock())

    llm = _stub_llm('{"route": "chat", "answer": "你好！有什么需要我帮忙的？"}')
    node = graph_mod._make_planner_node(llm)
    out = node(_planner_state("sc", "你好呀"))

    assert out["route"] == "chat"
    assert out["response_text"] == "你好！有什么需要我帮忙的？"
    assert out["planned_tasks"] == []
    assert out["early_exit"] is False


def test_planner_route_define_rule(monkeypatch):
    """route=define_rule 时 rule 字段被填充。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([LED_CARD]))
    monkeypatch.setattr(_t, "do_publish_feedback", MagicMock())

    rule = {
        "name": "变暗自动开灯",
        "trigger": {"agent_tag": "light_level", "event": "dark"},
        "action": {"resource_tag": "lighting", "cmd": "SET_STATE", "params": {"state": "BRIGHT"}},
    }
    llm = _stub_llm(json.dumps({"route": "define_rule", "rule": rule}, ensure_ascii=False))
    node = graph_mod._make_planner_node(llm)
    out = node(_planner_state("sr", "以后变暗就开灯"))

    assert out["route"] == "define_rule"
    assert out["rule"] == rule
    assert out["planned_tasks"] == []


def test_planner_unparseable_falls_back_to_chat(monkeypatch):
    """LLM 输出无法解析 → 默认 act → 无合法 tasks → 降级为 chat（友好回复，不崩条件边）。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([LED_CARD]))
    monkeypatch.setattr(_t, "do_publish_feedback", MagicMock())

    llm = _stub_llm("抱歉我不知道")
    node = graph_mod._make_planner_node(llm)
    out = node(_planner_state("sx", "???"))

    assert out["route"] == "chat"
    assert out["planned_tasks"] == []


def test_planner_unknown_route_falls_back_to_chat(monkeypatch):
    """route 不在已知值且 tasks 为空 → 默认 act → 降级为 chat，不让条件边崩溃。"""
    monkeypatch.setattr(_t, "_registry", FakeRegistry([LED_CARD]))
    monkeypatch.setattr(_t, "do_publish_feedback", MagicMock())

    llm = _stub_llm('{"route": "UNKNOWN", "tasks": []}')
    node = graph_mod._make_planner_node(llm)
    out = node(_planner_state("su", "???"))

    assert out["route"] == "chat"
    assert out["planned_tasks"] == []


# ── ChatNode / RuleBuilderNode 测试 ──────────────────────────────

def test_chat_node_publishes_done(monkeypatch):
    """ChatNode 调用 do_publish_feedback(stage="done") 发出 answer。"""
    fb = MagicMock()
    monkeypatch.setattr(_t, "do_publish_feedback", fb)

    node = graph_mod._make_chat_node(MagicMock())
    state = _planner_state("sc", "你好")
    state["route"] = "chat"
    state["response_text"] = "你好！"
    node(state)

    fb.assert_called_once()
    args = fb.call_args.args
    assert args[0] == "sc"       # session_id
    assert args[1] == "done"     # stage
    assert args[2] == "你好！"   # text


def test_rule_builder_node_publishes_pending_rule(monkeypatch):
    """RuleBuilderNode 发 pending_rule，payload 含 rule。"""
    fb = MagicMock()
    monkeypatch.setattr(_t, "do_publish_feedback", fb)

    rule = {"name": "变暗自动开灯", "trigger": {}, "action": {}}
    node = graph_mod._make_rule_builder_node()
    state = _planner_state("sr", "以后变暗就开灯")
    state["route"] = "define_rule"
    state["rule"] = rule
    node(state)

    fb.assert_called_once()
    assert fb.call_args.args[0] == "sr"
    assert fb.call_args.args[1] == "pending_rule"
    assert fb.call_args.kwargs["rule"] == rule


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
            {"unit_id": "esp32_desk_led", "skill_id": "set_light_state",
             "task_id": "s1_t0", "params": {"state": "BRIGHT"}}
        ],
        "task_results": {}, "early_exit": False,
    }
    out = node(state)

    pub.assert_called_once()
    args = pub.call_args.args
    assert args[0] == "esp32_desk_led"   # unit_id
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
            {"unit_id": "go2", "skill_id": "go2_navigate",
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
            {"unit_id": "go2", "skill_id": "go2_sport",
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
            {"unit_id": "go2", "skill_id": "go2_sport",
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
            {"unit_id": "go2", "skill_id": "go2_sport", "task_id": "s_t0", "params": {}},
            {"unit_id": "esp32_desk_led", "skill_id": "set_light_state", "task_id": "s_t1", "params": {}},
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


# ── 整图条件分支测试 ─────────────────────────────────────────────

def _run_graph(monkeypatch, llm_content, cards):
    """用 stub LLM 编译整图并跑一次，返回各节点 mock 以便断言路由。"""
    monkeypatch.setattr(graph_mod, "_make_llm", lambda: _stub_llm(llm_content))
    monkeypatch.setattr(_t, "_registry", FakeRegistry(cards))
    monkeypatch.setattr(_t, "do_publish_feedback", MagicMock())
    monkeypatch.setattr(_t, "do_publish_task", MagicMock())
    monkeypatch.setattr(_t, "do_http_dispatch", MagicMock(return_value={"result": "ok"}))

    # 各分支节点替身：记录是否被走到
    disp = MagicMock(side_effect=lambda s: s)
    chat = MagicMock(side_effect=lambda s: s)
    rule = MagicMock(side_effect=lambda s: s)
    eval_ = MagicMock(side_effect=lambda s: s)
    resp = MagicMock(side_effect=lambda s: s)
    monkeypatch.setattr(graph_mod, "_make_dispatcher_node", lambda: disp)
    monkeypatch.setattr(graph_mod, "_make_evaluator_node", lambda: eval_)
    monkeypatch.setattr(graph_mod, "_make_responder_node", lambda llm: resp)
    monkeypatch.setattr(graph_mod, "_make_chat_node", lambda llm: chat)
    monkeypatch.setattr(graph_mod, "_make_rule_builder_node", lambda: rule)

    app = graph_mod.build_orchestrator()
    app.invoke(_planner_state("g1", "测试"))
    return {"dispatcher": disp, "chat": chat, "rule_builder": rule,
            "evaluator": eval_, "responder": resp}


def test_graph_routes_act_to_dispatcher(monkeypatch):
    """route=act → 走 Dispatcher（不走 ChatNode / RuleBuilderNode）。"""
    nodes = _run_graph(
        monkeypatch,
        '{"route": "act", "tasks": [{"unit_id": "esp32_desk_led", '
        '"skill_id": "set_light_state", "params": {"state": "BRIGHT"}}]}',
        [LED_CARD],
    )
    nodes["dispatcher"].assert_called_once()
    nodes["chat"].assert_not_called()
    nodes["rule_builder"].assert_not_called()


def test_graph_routes_chat_to_chat_node(monkeypatch):
    """route=chat → 走 ChatNode（不走 Dispatcher / RuleBuilderNode）。"""
    nodes = _run_graph(
        monkeypatch,
        '{"route": "chat", "answer": "你好！"}',
        [LED_CARD],
    )
    nodes["chat"].assert_called_once()
    nodes["dispatcher"].assert_not_called()
    nodes["rule_builder"].assert_not_called()


def test_graph_routes_define_rule_to_rule_builder(monkeypatch):
    """route=define_rule → 走 RuleBuilderNode（不走 Dispatcher / ChatNode）。"""
    nodes = _run_graph(
        monkeypatch,
        '{"route": "define_rule", "rule": {"name": "变暗自动开灯", "trigger": {}, "action": {}}}',
        [LED_CARD],
    )
    nodes["rule_builder"].assert_called_once()
    nodes["dispatcher"].assert_not_called()
    nodes["chat"].assert_not_called()
