"""cloud.go2.tests.test_card — 验证 _build_go2_card 返回的结构符合 AgentCard 规格。"""

import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def restore_publisher(monkeypatch):
    from cloud.go2 import card
    for field in ("go2", "_mqtt_client", "_last_status_online"):
        monkeypatch.setattr(card, field, getattr(card, field))


def _load_card_with_stub():
    from cloud.go2 import card
    stub = MagicMock()
    stub.is_connected = True
    card.go2 = stub
    card.init_mqtt(None)
    return card, stub


class TestBuildGo2Card:
    """验证 _build_go2_card 返回的数据结构符合 AgentCard spec。"""

    def setup_method(self):
        """每个测试前重新加载 router（带 stub）。"""
        self.router_mod, self.go2_stub = _load_card_with_stub()

    def test_top_level_fields_present(self):
        """card 必须包含 unit_id / name / description / agent_type / online / transport / skills / state。"""
        card = self.router_mod._build_go2_card()
        for field in ("unit_id", "name", "description", "agent_type", "card_schema_version", "version", "device_id", "transport", "skills"):
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
        """Card 声明全部面向用户的公开技能。"""
        card = self.router_mod._build_go2_card()
        skill_ids = {s["id"] for s in card["skills"]}
        assert skill_ids == {"go2_chat", "go2_sport", "go2_navigate", "go2_recognize_person",
                             "go2_move", "go2_observe", "go2_status", "go2_set_led",
                             "go2_list_locations", "go2_tag_location", "go2_find_object",
                             "go2_visual_navigate", "go2_life"}

    def test_identity_card_exposes_only_read_capability(self):
        card = self.router_mod._build_go2_card()
        identity_ids = {s["id"] for s in card["skills"] if "identity" in s["tags"]}
        assert identity_ids == {"go2_recognize_person"}

    def test_skills_have_required_fields(self):
        """每个 skill 必须有 id / name / tags / params_schema / invoke 字段。"""
        card = self.router_mod._build_go2_card()
        for skill in card["skills"]:
            for field in ("id", "name", "tags", "params_schema", "invoke"):
                assert field in skill, f"skill '{skill.get('id')}' 缺少字段: {field}"

    def test_go2_sport_enum_values(self):
        """go2_sport skill 的 cmd enum 与统一动作目录一致。"""
        card = self.router_mod._build_go2_card()
        sport = next(s for s in card["skills"] if s["id"] == "go2_sport")
        enum_values = sport["params_schema"]["properties"]["cmd"]["enum"]
        from cloud.go2.actions import SPORT_COMMANDS
        assert set(enum_values) == set(SPORT_COMMANDS)

    def test_state_is_empty(self):
        """card 不含 volatile state（FSM/动作经 HTTP 实时获取，不进 retained card）。"""
        self.go2_stub.body_state = "standing"
        self.go2_stub.available_actions = ["Hello"]
        card = self.router_mod._build_go2_card()
        assert "state" not in card

    def test_online_is_runtime_projection(self):
        """发布者 card.online 不随 WebRTC 连接变化。"""
        assert "online" not in self.router_mod._build_go2_card()
        self.go2_stub.is_connected = False
        assert "online" not in self.router_mod._build_go2_card()

    def test_card_is_json_serializable(self):
        """card 必须可以被 json.dumps 序列化（用于 MQTT publish）。"""
        card = self.router_mod._build_go2_card()
        serialized = json.dumps(card, ensure_ascii=False)
        restored = json.loads(serialized)
        assert restored["unit_id"] == "go2"


class TestPublishGo2Card:
    """验证 _publish_go2_card 和 _clear_go2_card 在各种客户端状态下的行为。"""

    def setup_method(self):
        self.router_mod, self.go2_stub = _load_card_with_stub()

    def test_publish_calls_mqtt_with_retain(self):
        """_publish_go2_card 在客户端注入后应调用 publish，retain=True。"""
        mock_client = MagicMock()
        self.router_mod.init_mqtt(mock_client)
        self.router_mod._publish_go2_card()
        mock_client.publish.assert_called_once()
        args, kwargs = mock_client.publish.call_args
        assert args[0] == "ssm/agents/go2/card"
        assert kwargs.get("retain") is True or (len(args) >= 3 and args[2] is True)

    def test_publish_silent_when_no_client(self):
        """_mqtt_client 为 None 时，_publish_go2_card 静默失败，不抛异常。"""
        self.router_mod.init_mqtt(None)
        self.router_mod._publish_go2_card()  # 不应抛异常



def test_go2_card_has_state_machine():
    router_mod, _ = _load_card_with_stub()
    card = router_mod._build_go2_card()
    sm = next(region for region in card["state_machine"]["regions"] if region["id"] == "body")
    assert sm is not None
    assert "standing" in sm["states"]
    assert "moving" in sm["states"]
    # 进入移动控制由状态机负责，带速度的 Move 由摇杆数据面发送。
    t = next(x for x in sm["transitions"]
             if x["src"] == "standing" and x["trigger"] == "EnterMoveControl")
    assert t["dst"] == "standing"
    assert t["label"]
    assert sm["views"]["standing"]["type"] == "robot_pose"
    assert set(sm["layout"]) == set(sm["states"])
    assert sm["views"]["greeting"]["pose"] == "greeting"


def test_card_whitelist_uses_tool_registry_schema():
    from cloud.go2.tools.registry import CARD_SKILLS, get_card_skill
    from cloud.go2.tools.registry import get_tool_spec

    assert get_card_skill("free_explore") is None
    assert get_card_skill("go2_observe") is not None
    for skill_id, entry in CARD_SKILLS.items():
        tool_name = entry["tool_name"]
        if tool_name is not None:
            assert get_card_skill(skill_id)["params_schema"] == get_tool_spec(tool_name).params_schema
    card, _ = _load_card_with_stub()
    assert all("tool_name" not in skill for skill in card._build_go2_card()["skills"])


def test_validator_keeps_region_state_machine():
    from cloud.cards.validation import normalize_card
    router_mod, _ = _load_card_with_stub()
    payload = router_mod._build_go2_card()
    assert normalize_card(payload)["state_machine"] == payload["state_machine"]


def test_go2_card_含_autonomy_modes_与_widgets():
    """Go2 card 应带 autonomy 模式轴、telemetry 字段与 joystick/video 富控件。"""
    router_mod, _ = _load_card_with_stub()
    card = router_mod._build_go2_card()
    modes = card.get("modes")
    assert modes and modes[0]["id"] == "autonomy"
    vals = [o["value"] for o in modes[0]["options"]]
    assert vals == ["manual", "free_explore"]
    assert modes[0]["set_action"] == "set_mode"
    assert any(t["key"] == "body_state" for t in card.get("telemetry", []))
    widgets = card.get("widgets", [])
    types = [w["type"] for w in widgets]
    assert "joystick" in types and "video" in types
    joystick = next(w for w in widgets if w["type"] == "joystick")
    assert joystick["states"] == ["standing", "moving"]
    assert joystick["placement"] == "controls"
    assert next(w for w in widgets if w["type"] == "video")["placement"] == "media"
    conn = next(w for w in widgets if w["type"] == "connection")
    assert conn["auto_connect"] is True
    assert conn["visible"] is False


def test_state_machine_has_action_states_not_executing():
    """状态机含 4 个动作态、不含 executing，且 standing--Hello-->greeting 存在。"""
    router_mod, _ = _load_card_with_stub()
    from cloud.go2.card import _go2_state_machine
    sm = next(region for region in _go2_state_machine()["regions"] if region["id"] == "body")
    names = set(sm["states"])
    assert {"greeting", "stretching", "dancing1", "dancing2"} <= names
    assert "executing" not in names
    assert any(
        t["src"] == "standing" and t["trigger"] == "Hello" and t["dst"] == "greeting"
        for t in sm["transitions"]
    )


def test_emergency_stop_is_a_safety_transition_to_real_state():
    router_mod, _ = _load_card_with_stub()
    from cloud.go2.card import _go2_state_machine
    sm = next(region for region in _go2_state_machine()["regions"] if region["id"] == "body")
    assert "emergency_stop" not in sm["states"]
    stops = {t["src"]: t for t in sm["transitions"] if t.get("safety")}
    assert set(stops) == set(sm["states"])
    assert all(t["action"] == "STOP" and t["label"] == "紧急制停" for t in stops.values())
    assert all("display_node" not in t for t in stops.values())
    assert "emergency_stop" not in sm["views"]
    assert stops["moving"]["dst"] == "moving"
    assert stops["standing"]["dst"] == "standing"
    assert stops["lying"]["dst"] == "lying"
    assert stops["sitting"]["dst"] == "sitting"


def test_video_widget_states_drop_executing():
    """video 富控件显示状态对齐动作态，不再引用已删除的 executing。"""
    router_mod, _ = _load_card_with_stub()
    card = router_mod._build_go2_card()
    video = next(w for w in card["widgets"] if w["type"] == "video")
    assert "executing" not in video["states"]
    assert video["states"] == []


def test_actual_go2_card_passes_shared_v1_validator():
    from cloud.cards.validation import normalize_card
    from cloud.api.devices import _readable_card

    router_mod, _ = _load_card_with_stub()
    card = router_mod._build_go2_card()
    assert card["card_schema_version"] == 1
    assert "state_field" not in card["state_machine"]
    assert card["state_machine"]["connection_field"] == "connected"
    assert normalize_card(card) == card
    assert all(skill["description"] for skill in card["skills"])
    assert json.loads(_readable_card(card).body) == card


def test_publish_go2_status_reports_service_presence():
    router_mod, go2_stub = _load_card_with_stub()

    class FakeMqtt:
        def __init__(self):
            self.calls = []

        def publish(self, topic, payload, retain=False, qos=0):
            self.calls.append((topic, payload, retain, qos))

    fake = FakeMqtt()
    router_mod.init_mqtt(fake)

    go2_stub.is_connected = False
    router_mod._publish_go2_status()
    assert fake.calls[-1] == (router_mod.GO2_STATUS_TOPIC, "online", True, 1)

    go2_stub.is_connected = True
    router_mod._publish_go2_status()
    assert fake.calls[-1] == (router_mod.GO2_STATUS_TOPIC, "online", True, 1)
