"""ESP32 card 暴露 chat_endpoint 的测试。"""
from cloud.cards.builder import build_card_from_manifest


def test_led_card_has_chat_endpoint():
    manifest = {
        "unit_id": "esp32_desk_led", "name": "桌面灯", "agent_type": "actuator",
        "capabilities": [{"action": "SET_STATE"}], "fsm": "led",
    }
    card = build_card_from_manifest(manifest)
    assert card["transport"]["chat_endpoint"] == "/api/esp32/esp32_desk_led/chat"


def test_sensor_card_also_has_chat_endpoint():
    """chat_endpoint 对所有 esp32 unit 注入（单一 ESP32 智能体处理）。"""
    manifest = {
        "unit_id": "esp32_desk_light", "name": "光线传感器", "agent_type": "sensor",
        "capabilities": [],
    }
    card = build_card_from_manifest(manifest)
    assert card["transport"]["chat_endpoint"] == "/api/esp32/esp32_desk_light/chat"
