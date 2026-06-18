"""ESP32 灯 card 暴露 chat_endpoint 的测试。"""
from cloud.cards.builder import build_card_from_manifest


def test_led_card_has_chat_endpoint():
    manifest = {
        "unit_id": "esp32_desk_led", "name": "桌面灯", "agent_type": "actuator",
        "capabilities": [{"action": "SET_STATE"}], "fsm": "led",
    }
    card = build_card_from_manifest(manifest)
    assert card["transport"]["chat_endpoint"] == "/api/esp32/esp32_desk_led/chat"
