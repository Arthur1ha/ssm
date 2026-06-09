"""cloud.cards.tests.test_builder — build_card_from_manifest 和 parse_card 的单元测试。"""

import pytest
from cloud.cards.builder import build_card_from_manifest, parse_card


# ── 测试 fixtures ──────────────────────────────────────────────

LED_MANIFEST = {
    "unit_id": "esp32_desk_led",
    "parent_id": "esp32_desk",
    "agent_type": "actuator",
    "name": "ws2812_ring",
    "hw_platform": "esp32",
    "slug": "desk-lamp",
    "commands": ["SET_COLOR", "SET_STATE", "BLINK"],
    "capabilities": [
        {"action": "SET_COLOR", "params": ["r", "g", "b", "brightness"]},
        {"action": "SET_STATE", "params": ["state"], "values": ["OFF", "BRIGHT", "DIM"]},
        {"action": "BLINK",     "params": ["r", "g", "b", "count"]},
    ],
    "resource_tags": ["lighting", "ambiance"],
    "ts": 1780709800,
}

LIGHT_SENSOR_MANIFEST = {
    "unit_id": "esp32_desk_light",
    "parent_id": "esp32_desk",
    "agent_type": "sensor",
    "name": "ambient_light",
    "hw_platform": "esp32",
    "agent_tag": "light_level",
    "levels": ["DARK", "DIM", "NORMAL", "BRIGHT"],
    "ts": 1780709801,
}

GO2_CARD = {
    "slug": "go2",
    "name": "Go2 机器狗",
    "description": "四足机器人，支持运动控制和视觉感知",
    "agent_type": "robot",
    "online": True,
    "transport": {"kind": "http", "endpoint": "http://localhost:8082"},
    "skills": [
        {
            "id": "go2_sport",
            "name": "执行预定义动作",
            "tags": ["actuator", "robot"],
            "params_schema": {
                "type": "object",
                "required": ["cmd"],
                "properties": {"cmd": {"type": "string"}},
            },
            "invoke": {"action": ""},
        }
    ],
    "state": {"fsm": "idle"},
}


# ── actuator（LED）测试 ──────────────────────────────────────────

class TestBuildCardFromManifestActuator:
    """测试 build_card_from_manifest 对 actuator 设备的处理。"""

    def setup_method(self):
        """每个测试前构建 LED card。"""
        self.card = build_card_from_manifest(LED_MANIFEST)

    def test_slug_来自_manifest(self):
        """slug 应使用 manifest['slug'] 字段。"""
        assert self.card["slug"] == "desk-lamp"

    def test_name_正确(self):
        """name 应对应 manifest['name']。"""
        assert self.card["name"] == "ws2812_ring"

    def test_agent_type_正确(self):
        """agent_type 应为 actuator。"""
        assert self.card["agent_type"] == "actuator"

    def test_online_为_True(self):
        """收到 manifest 即表示设备在线。"""
        assert self.card["online"] is True

    def test_transport_为_mqtt(self):
        """ESP32 设备传输层应为 mqtt。"""
        assert self.card["transport"]["kind"] == "mqtt"

    def test_transport_task_topic_含_slug(self):
        """task_topic 应包含 slug。"""
        assert "desk-lamp" in self.card["transport"]["task_topic"]

    def test_skills_数量_为_三个(self):
        """LED manifest 有三条 capabilities，应产出三个 skill。"""
        assert len(self.card["skills"]) == 3

    def test_SET_STATE_skill_id_正确(self):
        """SET_STATE 对应 skill id 为 set_light_state。"""
        ids = [s["id"] for s in self.card["skills"]]
        assert "set_light_state" in ids

    def test_SET_COLOR_skill_id_正确(self):
        """SET_COLOR 对应 skill id 为 set_light_color。"""
        ids = [s["id"] for s in self.card["skills"]]
        assert "set_light_color" in ids

    def test_BLINK_skill_id_正确(self):
        """BLINK 对应 skill id 为 blink_light。"""
        ids = [s["id"] for s in self.card["skills"]]
        assert "blink_light" in ids

    def test_SET_STATE_invoke_action_正确(self):
        """SET_STATE skill 的 invoke.action 应为 SET_STATE。"""
        skill = next(s for s in self.card["skills"] if s["id"] == "set_light_state")
        assert skill["invoke"]["action"] == "SET_STATE"

    def test_SET_COLOR_invoke_action_正确(self):
        """SET_COLOR skill 的 invoke.action 应为 SET_COLOR。"""
        skill = next(s for s in self.card["skills"] if s["id"] == "set_light_color")
        assert skill["invoke"]["action"] == "SET_COLOR"

    def test_BLINK_invoke_action_正确(self):
        """BLINK skill 的 invoke.action 应为 BLINK。"""
        skill = next(s for s in self.card["skills"] if s["id"] == "blink_light")
        assert skill["invoke"]["action"] == "BLINK"

    def test_state_为空(self):
        """builder 不填充动态状态，state 应为空 dict。"""
        assert self.card["state"] == {}


# ── sensor（光线传感器）测试 ────────────────────────────────────

class TestBuildCardFromManifestSensor:
    """测试 build_card_from_manifest 对 sensor 设备的处理。"""

    def setup_method(self):
        """每个测试前构建光线传感器 card。"""
        self.card = build_card_from_manifest(LIGHT_SENSOR_MANIFEST)

    def test_slug_回退到_unit_id(self):
        """无 slug 字段时应回退到 unit_id。"""
        assert self.card["slug"] == "esp32_desk_light"

    def test_agent_type_为_sensor(self):
        """agent_type 应为 sensor。"""
        assert self.card["agent_type"] == "sensor"

    def test_skills_为空列表(self):
        """sensor 无 capabilities，skills 应为空列表。"""
        assert self.card["skills"] == []

    def test_online_为_True(self):
        """收到 manifest 即表示设备在线。"""
        assert self.card["online"] is True


# ── 未知 action 跳过测试 ────────────────────────────────────────

def test_未知_action_静默跳过():
    """manifest 中未知 action 不应报错，只是跳过。"""
    manifest = {
        "unit_id": "test_device",
        "agent_type": "actuator",
        "capabilities": [
            {"action": "SET_STATE", "params": ["state"]},
            {"action": "UNKNOWN_ACTION", "params": []},
        ],
    }
    card = build_card_from_manifest(manifest)
    assert len(card["skills"]) == 1
    assert card["skills"][0]["id"] == "set_light_state"


# ── parse_card 测试 ─────────────────────────────────────────────

class TestParseCard:
    """测试 parse_card 对自描述 card payload 的处理。"""

    def setup_method(self):
        """每个测试前解析 Go2 card。"""
        self.card = parse_card(GO2_CARD)

    def test_slug_正确(self):
        """slug 应保持原值。"""
        assert self.card["slug"] == "go2"

    def test_name_正确(self):
        """name 应保持原值。"""
        assert self.card["name"] == "Go2 机器狗"

    def test_agent_type_正确(self):
        """agent_type 应保持原值。"""
        assert self.card["agent_type"] == "robot"

    def test_online_正确(self):
        """online 状态应保持原值。"""
        assert self.card["online"] is True

    def test_transport_正确(self):
        """transport 应保持原值。"""
        assert self.card["transport"]["kind"] == "http"

    def test_skills_不为空(self):
        """Go2 card 有 skills，应正确传递。"""
        assert len(self.card["skills"]) == 1

    def test_state_正确(self):
        """state 应保持原值。"""
        assert self.card["state"]["fsm"] == "idle"
