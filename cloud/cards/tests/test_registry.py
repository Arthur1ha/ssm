"""cloud.cards.tests.test_registry — CardRegistry 的单元测试。"""

import json
import pytest
from cloud.cards.registry import CardRegistry


# ── fixtures ────────────────────────────────────────────────────

LED_MANIFEST = {
    "unit_id": "esp32_desk_led",
    "parent_id": "esp32_desk",
    "agent_type": "actuator",
    "name": "ws2812_ring",
    "hw_platform": "esp32",
    "slug": "desk-lamp",
    "capabilities": [
        {"action": "SET_COLOR", "params": ["r", "g", "b", "brightness"]},
        {"action": "SET_STATE", "params": ["state"], "values": ["OFF", "BRIGHT", "DIM"]},
        {"action": "BLINK",     "params": ["r", "g", "b", "count"]},
    ],
    "resource_tags": ["lighting", "ambiance"],
    "ts": 1780709800,
}

GO2_CARD = {
    "slug": "go2",
    "name": "Go2 机器狗",
    "description": "四足机器人",
    "agent_type": "robot",
    "online": True,
    "transport": {"kind": "http"},
    "skills": [],
    "state": {},
}


@pytest.fixture
def registry():
    """每个测试用独立的 CardRegistry 实例，避免状态污染。"""
    return CardRegistry()


# ── manifest topic 测试 ─────────────────────────────────────────

class TestHandleManifest:
    """测试收到 manifest topic 后 registry 的状态。"""

    def test_收到_manifest_card_出现在_get_all_cards(self, registry):
        """收到 manifest 后 get_all_cards 应包含对应 card。"""
        registry.handle_message(
            "ssm/agents/esp32_desk_led/manifest",
            json.dumps(LED_MANIFEST).encode(),
        )
        cards = registry.get_all_cards()
        assert "esp32_desk_led" in cards

    def test_manifest_card_以_unit_id_为_key(self, registry):
        """card 以 unit_id 为 key，忽略 manifest['slug']。"""
        registry.handle_message(
            "ssm/agents/esp32_desk_led/manifest",
            json.dumps(LED_MANIFEST).encode(),
        )
        cards = registry.get_all_cards()
        assert "esp32_desk_led" in cards
        assert "desk-lamp" not in cards   # slug 不再是 key

    def test_manifest_card_agent_type_正确(self, registry):
        """从 manifest 构建的 card agent_type 应为 actuator。"""
        registry.handle_message(
            "ssm/agents/esp32_desk_led/manifest",
            json.dumps(LED_MANIFEST).encode(),
        )
        card = registry.get_card("esp32_desk_led")
        assert card["agent_type"] == "actuator"

    def test_manifest_card_有_skills(self, registry):
        """从 manifest 构建的 actuator card 应有 skills。"""
        registry.handle_message(
            "ssm/agents/esp32_desk_led/manifest",
            json.dumps(LED_MANIFEST).encode(),
        )
        card = registry.get_card("esp32_desk_led")
        assert len(card["skills"]) == 4  # 3 capabilities + set_autonomy 模式 skill

    def test_空_payload_manifest_被忽略(self, registry):
        """空 payload 的 manifest 消息应静默忽略，不报错。"""
        registry.handle_message("ssm/agents/esp32_desk_led/manifest", b"")
        assert registry.get_card("esp32_desk_led") is None

    def test_无效_json_manifest_被忽略(self, registry):
        """无效 JSON 的 manifest 应静默忽略，不抛异常。"""
        registry.handle_message("ssm/agents/esp32_desk_led/manifest", b"not-json")
        assert len(registry.get_all_cards()) == 0


# ── card topic 测试 ─────────────────────────────────────────────

class TestHandleCard:
    """测试收到 card topic 后 registry 的状态。"""

    def test_收到_card_get_card_返回正确结果(self, registry):
        """收到自描述 card 后 get_card 应返回该 card。"""
        registry.handle_message(
            "ssm/agents/go2/card",
            json.dumps(GO2_CARD).encode(),
        )
        card = registry.get_card("go2")
        assert card is not None
        assert card["unit_id"] == "go2"

    def test_收到_card_agent_type_正确(self, registry):
        """自描述 card 的 agent_type 应保持原值。"""
        registry.handle_message(
            "ssm/agents/go2/card",
            json.dumps(GO2_CARD).encode(),
        )
        card = registry.get_card("go2")
        assert card["agent_type"] == "robot"

    def test_收到_空_payload_card_slug_被移除(self, registry):
        """收到空 payload 的 card 消息后，对应 slug 应从注册表移除。"""
        # 先插入
        registry.handle_message(
            "ssm/agents/go2/card",
            json.dumps(GO2_CARD).encode(),
        )
        assert registry.get_card("go2") is not None

        # 再发空 payload
        registry.handle_message("ssm/agents/go2/card", b"")
        assert registry.get_card("go2") is None

    def test_收到_空字符串_payload_card_slug_被移除(self, registry):
        """空字符串 payload 也应触发移除。"""
        registry.handle_message(
            "ssm/agents/go2/card",
            json.dumps(GO2_CARD).encode(),
        )
        registry.handle_message("ssm/agents/go2/card", "")
        assert registry.get_card("go2") is None

    def test_card_出现在_get_all_cards(self, registry):
        """自描述 card 应出现在 get_all_cards 返回值中。"""
        registry.handle_message(
            "ssm/agents/go2/card",
            json.dumps(GO2_CARD).encode(),
        )
        assert "go2" in registry.get_all_cards()


# ── get_all_cards 副本测试 ──────────────────────────────────────

class TestGetAllCardsCopy:
    """测试 get_all_cards 返回副本，修改不影响内部状态。"""

    def test_修改返回值不影响内部状态(self, registry):
        """get_all_cards 返回副本，修改外部变量不应影响 registry 内部。"""
        registry.handle_message(
            "ssm/agents/go2/card",
            json.dumps(GO2_CARD).encode(),
        )

        cards_copy = registry.get_all_cards()
        cards_copy["injected"] = {"slug": "injected"}  # 修改副本

        # 内部状态不应受影响
        assert registry.get_card("injected") is None
        assert len(registry.get_all_cards()) == 1

    def test_返回值是_dict(self, registry):
        """get_all_cards 应始终返回 dict。"""
        assert isinstance(registry.get_all_cards(), dict)


# ── 无关 topic 忽略测试 ─────────────────────────────────────────

class TestIgnoreIrrelevantTopics:
    """测试无关 topic 被静默忽略。"""

    def test_无关_topic_被忽略(self, registry):
        """非 card/manifest topic 应静默忽略。"""
        registry.handle_message("ssm/agents/esp32_desk_led/state", b'{"level": "DIM"}')
        assert len(registry.get_all_cards()) == 0

    def test_格式不符_topic_被忽略(self, registry):
        """格式不符合 ssm/agents/+/type 的 topic 应静默忽略。"""
        registry.handle_message("ssm/result/abc/123", b'{"ok": true}')
        assert len(registry.get_all_cards()) == 0


# ── status / 在线状态测试 ───────────────────────────────────────

class TestStatusOnline:
    """测试按 /status topic（含 LWT）维护 card.online。"""

    def test_父设备_status_offline_使子单元离线(self, registry):
        """父设备 LWT 触发 status=offline → 子单元 card.online 转 False，再 online 恢复。"""
        registry.handle_message(
            "ssm/agents/esp32_desk_led/manifest", json.dumps(LED_MANIFEST).encode())
        assert registry.get_card("esp32_desk_led")["online"] is True

        registry.handle_message("ssm/agents/esp32_desk/status", b"offline")
        assert registry.get_card("esp32_desk_led")["online"] is False

        registry.handle_message("ssm/agents/esp32_desk/status", b"online")
        assert registry.get_card("esp32_desk_led")["online"] is True

    def test_manifest_重发不覆盖_offline(self, registry):
        """status=offline 后即便 manifest 重发，online 仍保持 False（status 是真相）。"""
        registry.handle_message(
            "ssm/agents/esp32_desk_led/manifest", json.dumps(LED_MANIFEST).encode())
        registry.handle_message("ssm/agents/esp32_desk/status", b"offline")
        registry.handle_message(
            "ssm/agents/esp32_desk_led/manifest", json.dumps(LED_MANIFEST).encode())
        registry.handle_message("ssm/agents/esp32_desk/status", b"offline")
        registry.handle_message(
            "ssm/agents/esp32_desk_led/manifest", json.dumps(LED_MANIFEST).encode())
        assert registry.get_card("esp32_desk_led")["online"] is False

    def test_card_重发不覆盖_offline(self, registry):
        """竞态：status=offline 后即便 retained card（online:true）后到，online 仍保持 False。"""
        # 先插入自描述 card（card 内硬编码 online:true）
        registry.handle_message("ssm/agents/go2/card", json.dumps(GO2_CARD).encode())
        # status=offline 先到，将 online 置为真相 False
        registry.handle_message("ssm/agents/go2/status", b"offline")
        assert registry.get_card("go2")["online"] is False
        # 后到的 retained card（online:true）不应覆盖 status 派生的离线真相
        registry.handle_message("ssm/agents/go2/card", json.dumps(GO2_CARD).encode())
        assert registry.get_card("go2")["online"] is False


class TestEmptyManifestRemoves:
    """测试空 manifest（缺席单元）移除对应 card。"""

    IR_MANIFEST = {
        "unit_id": "esp32_desk_ir", "parent_id": "esp32_desk",
        "agent_type": "sensor", "name": "ir_presence",
        "hw_platform": "esp32", "capabilities": [], "tags": ["presence"],
    }

    def test_空_manifest_移除_card(self, registry):
        registry.handle_message(
            "ssm/agents/esp32_desk_ir/manifest", json.dumps(self.IR_MANIFEST).encode())
        assert registry.get_card("esp32_desk_ir") is not None
        registry.handle_message("ssm/agents/esp32_desk_ir/manifest", b"")
        assert registry.get_card("esp32_desk_ir") is None


# ── 多设备并存测试 ──────────────────────────────────────────────

def test_多设备并存(registry):
    """registry 应支持同时存储多个设备的 card。"""
    registry.handle_message(
        "ssm/agents/esp32_desk_led/manifest",
        json.dumps(LED_MANIFEST).encode(),
    )
    registry.handle_message(
        "ssm/agents/go2/card",
        json.dumps(GO2_CARD).encode(),
    )
    cards = registry.get_all_cards()
    assert "esp32_desk_led" in cards
    assert "go2" in cards
    assert len(cards) == 2
