"""cloud.go2.tests.test_card — 验证 _build_go2_card 返回的结构符合 AgentCard 规格。"""

import json
import sys
import types
from unittest.mock import MagicMock, patch


def _make_go2_stub():
    """构造一个最小 go2 连接对象 stub，避免引入真实 WebRTC 依赖。"""
    stub = MagicMock()
    stub.fsm_state = "idle"
    stub.available_actions = ["StandUp", "StandDown"]
    stub.is_connected = True   # card.online 现在反映实际连接状态，需为真实 bool
    return stub


def _load_router_with_stub():
    """用 stub 替换重量级依赖后加载 cloud.go2.router，返回模块对象。"""
    import importlib
    go2_stub = _make_go2_stub()

    # cloud.go2.connection.fsm 只依赖 asyncio/logging，直接用真实模块，无需 stub
    real_fsm = importlib.import_module("cloud.go2.connection.fsm")

    # 构造最小假包结构，避免实际 import 引入 ultralytics / WebRTC 等
    patches = {
        "cloud.go2.connection":                   types.ModuleType("cloud.go2.connection"),
        "cloud.go2.connection.fsm":               real_fsm,
        "cloud.go2.navigation.drive":              types.ModuleType("cloud.go2.navigation.drive"),
        "cloud.go2.agentcore.skills.reactive":     types.ModuleType("cloud.go2.agentcore.skills.reactive"),
        "cloud.go2.agentcore.skills.vision":       types.ModuleType("cloud.go2.agentcore.skills.vision"),
    }
    patches["cloud.go2.connection"].go2 = go2_stub
    patches["cloud.go2.navigation.drive"].drive = MagicMock()
    patches["cloud.go2.agentcore.skills.reactive"].reactive_mind = MagicMock()
    patches["cloud.go2.agentcore.skills.vision"].vision_loop = MagicMock()

    with patch.dict(sys.modules, patches):
        # 强制重新加载，确保 stub 生效
        if "cloud.go2.router" in sys.modules:
            del sys.modules["cloud.go2.router"]
        import cloud.go2.router as router_mod
        return router_mod, go2_stub


class TestBuildGo2Card:
    """验证 _build_go2_card 返回的数据结构符合 AgentCard spec。"""

    def setup_method(self):
        """每个测试前重新加载 router（带 stub）。"""
        self.router_mod, self.go2_stub = _load_router_with_stub()

    def test_top_level_fields_present(self):
        """card 必须包含 unit_id / name / description / agent_type / online / transport / skills / state。"""
        card = self.router_mod._build_go2_card()
        for field in ("unit_id", "name", "description", "agent_type", "online", "transport", "skills", "state"):
            assert field in card, f"缺少字段: {field}"

    def test_unit_id_is_go2(self):
        """unit_id 必须为 'go2'。"""
        card = self.router_mod._build_go2_card()
        assert card["unit_id"] == "go2"

    def test_transport_kind_and_endpoint(self):
        """transport.kind 必须为 'http'，endpoint 必须为 '/api/go2/chat'。"""
        card = self.router_mod._build_go2_card()
        assert card["transport"]["kind"] == "http"
        assert card["transport"]["endpoint"] == "/api/go2/chat"

    def test_skills_count_and_ids(self):
        """必须包含 go2_chat、go2_sport、go2_navigate 三个 skill。"""
        card = self.router_mod._build_go2_card()
        skill_ids = {s["id"] for s in card["skills"]}
        assert skill_ids == {"go2_chat", "go2_sport", "go2_navigate"}

    def test_skills_have_required_fields(self):
        """每个 skill 必须有 id / name / tags / params_schema / invoke 字段。"""
        card = self.router_mod._build_go2_card()
        for skill in card["skills"]:
            for field in ("id", "name", "tags", "params_schema", "invoke"):
                assert field in skill, f"skill '{skill.get('id')}' 缺少字段: {field}"

    def test_go2_sport_enum_values(self):
        """go2_sport skill 的 cmd enum 必须包含全部 6 个预定义动作。"""
        card = self.router_mod._build_go2_card()
        sport = next(s for s in card["skills"] if s["id"] == "go2_sport")
        enum_values = sport["params_schema"]["properties"]["cmd"]["enum"]
        assert set(enum_values) == {"StandUp", "StandDown", "Hello", "Stretch", "Dance1", "Dance2"}

    def test_state_is_empty(self):
        """card 不含 volatile state（FSM/动作经 HTTP 实时获取，不进 retained card）。"""
        self.go2_stub.fsm_state = "standing"
        self.go2_stub.available_actions = ["Hello"]
        card = self.router_mod._build_go2_card()
        assert card["state"] == {}

    def test_online_reflects_connection(self):
        """card online 字段反映实际连接状态：已连接为 True，断开为 False。"""
        # stub 默认 is_connected=True
        assert self.router_mod._build_go2_card()["online"] is True
        # 断开后应反映为 False（真相由 status topic 维护，card 不再硬编码 True）
        self.go2_stub.is_connected = False
        assert self.router_mod._build_go2_card()["online"] is False

    def test_card_is_json_serializable(self):
        """card 必须可以被 json.dumps 序列化（用于 MQTT publish）。"""
        card = self.router_mod._build_go2_card()
        serialized = json.dumps(card, ensure_ascii=False)
        restored = json.loads(serialized)
        assert restored["unit_id"] == "go2"


class TestPublishClearGo2Card:
    """验证 _publish_go2_card 和 _clear_go2_card 在各种客户端状态下的行为。"""

    def setup_method(self):
        self.router_mod, self.go2_stub = _load_router_with_stub()

    def test_publish_calls_mqtt_with_retain(self):
        """_publish_go2_card 在客户端注入后应调用 publish，retain=True。"""
        mock_client = MagicMock()
        self.router_mod.init_mqtt(mock_client)
        self.router_mod._publish_go2_card()
        mock_client.publish.assert_called_once()
        args, kwargs = mock_client.publish.call_args
        assert args[0] == "ssm/agents/go2/card"
        assert kwargs.get("retain") is True or (len(args) >= 3 and args[2] is True)

    def test_clear_publishes_empty_payload_with_retain(self):
        """_clear_go2_card 应 publish 空字符串，retain=True。"""
        mock_client = MagicMock()
        self.router_mod.init_mqtt(mock_client)
        self.router_mod._clear_go2_card()
        mock_client.publish.assert_called_once()
        args, kwargs = mock_client.publish.call_args
        assert args[0] == "ssm/agents/go2/card"
        assert args[1] == ""
        assert kwargs.get("retain") is True or (len(args) >= 3 and args[2] is True)

    def test_publish_silent_when_no_client(self):
        """_mqtt_client 为 None 时，_publish_go2_card 静默失败，不抛异常。"""
        self.router_mod._mqtt_client = None
        self.router_mod._publish_go2_card()  # 不应抛异常

    def test_clear_silent_when_no_client(self):
        """_mqtt_client 为 None 时，_clear_go2_card 静默失败，不抛异常。"""
        self.router_mod._mqtt_client = None
        self.router_mod._clear_go2_card()  # 不应抛异常


def test_go2_card_has_state_machine():
    router_mod, _ = _load_router_with_stub()
    card = router_mod._build_go2_card()
    sm = card.get("state_machine")
    assert sm is not None
    assert "standing" in sm["states"]
    assert "moving" in sm["states"]
    # standing --Move--> moving 这条转移存在且有 label
    t = next(x for x in sm["transitions"]
             if x["src"] == "standing" and x["trigger"] == "Move")
    assert t["dst"] == "moving"
    assert t["label"]


def test_parse_card_keeps_state_machine():
    from cloud.cards.builder import parse_card

    payload = {
        "unit_id": "go2", "name": "Go2", "agent_type": "robot",
        "online": True, "transport": {"kind": "http"}, "skills": [],
        "state": {},
        "state_machine": {"states": ["standing"], "transitions": []},
    }
    card = parse_card(payload)
    assert card.get("state_machine") == payload["state_machine"]


def test_go2_card_含_autonomy_modes_与_widgets():
    """Go2 card 应带 autonomy 模式轴、telemetry 字段与 joystick/video 富控件。"""
    from cloud.go2.router import _build_go2_card
    card = _build_go2_card()
    modes = card.get("modes")
    assert modes and modes[0]["id"] == "autonomy"
    vals = [o["value"] for o in modes[0]["options"]]
    assert "remote" in vals and "free_explore" in vals
    assert modes[0]["set"] == "/api/go2/autonomy"
    assert any(t["key"] == "fsm_state" for t in card.get("telemetry", []))
    types = [w["type"] for w in card.get("widgets", [])]
    assert "joystick" in types and "video" in types
