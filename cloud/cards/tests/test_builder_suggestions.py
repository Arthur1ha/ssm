"""card.suggestions：灯 manifest 注入快捷指令；自描述 card 透传 suggestions。"""
from cloud.cards.builder import build_card_from_manifest
from cloud.cards.validation import normalize_card
from cloud.cards.tests.test_builder import GO2_CARD


def test_led_card_has_suggestions():
    """灯 card 应带 card 驱动的快捷指令词列表。"""
    manifest = {
        "unit_id": "esp32_desk_led", "name": "桌面灯", "agent_type": "actuator",
        "capabilities": [{"action": "SET_STATE"}], "fsm_version": 3, "firmware_ver": "0.3.0",
    }
    card = build_card_from_manifest(manifest)
    assert isinstance(card["suggestions"], list)
    assert len(card["suggestions"]) > 0


def test_non_led_card_has_no_suggestions():
    """非灯设备无 fsm 映射时不应带 suggestions。"""
    manifest = {
        "unit_id": "esp32_desk_light", "name": "光线传感器", "agent_type": "sensor",
        "capabilities": [], "firmware_ver": "0.3.0",
    }
    card = build_card_from_manifest(manifest)
    assert "suggestions" not in card


def test_static_card_preserves_suggestions():
    """自描述 card（如 Go2）若声明 suggestions 则原样透传。"""
    payload = {**GO2_CARD, "suggestions": ["过来", "坐下"]}
    card = normalize_card(payload)
    assert card["suggestions"] == ["过来", "坐下"]
