"""ESP32 card 暴露 chat_endpoint 的测试。"""
from cloud.cards.builder import build_card_from_manifest


def test_led_card_has_chat_endpoint():
    manifest = {
        "unit_id": "esp32_desk_led", "name": "桌面灯", "agent_type": "actuator",
        "capabilities": [{"action": "SET_STATE"}], "fsm_version": 3, "firmware_ver": "0.3.0",
    }
    card = build_card_from_manifest(manifest)
    assert card["transport"]["kind"] == "mqtt"
    assert card["services"]["chat_endpoint"] == "/api/esp32/esp32_desk_led/chat"


def test_sensor_card_has_no_chat_endpoint():
    """感知单元不暴露灯智能体的对话入口。"""
    manifest = {
        "unit_id": "esp32_desk_light", "name": "光线传感器", "agent_type": "sensor",
        "capabilities": [], "firmware_ver": "0.3.0",
    }
    card = build_card_from_manifest(manifest)
    assert "services" not in card
