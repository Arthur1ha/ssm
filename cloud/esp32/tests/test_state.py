import pytest
from cloud.esp32.state import ESP32State


def make_state():
    return ESP32State()


class TestOnManifest:
    def test_stores_manifest(self):
        s = make_state()
        s.on_manifest("esp32_desk_led", {"unit_id": "esp32_desk_led", "resource_tags": ["lighting"]})
        assert s.get_manifest("esp32_desk_led") is not None

    def test_builds_capability_registry(self):
        s = make_state()
        s.on_manifest("esp32_desk_led", {"unit_id": "esp32_desk_led", "resource_tags": ["lighting"]})
        reg = s.get_capability_registry()
        assert "lighting" in reg
        assert "esp32_desk_led" in reg["lighting"]

    def test_multiple_tags(self):
        s = make_state()
        s.on_manifest("esp32_desk_led", {"resource_tags": ["lighting", "ambiance"]})
        reg = s.get_capability_registry()
        assert "esp32_desk_led" in reg["ambiance"]

    def test_no_duplicate_in_registry(self):
        s = make_state()
        manifest = {"resource_tags": ["lighting"]}
        s.on_manifest("esp32_desk_led", manifest)
        s.on_manifest("esp32_desk_led", manifest)
        assert s.get_capability_registry()["lighting"].count("esp32_desk_led") == 1


class TestOnAgentMsg:
    def test_sensor_stored_in_sensors(self):
        s = make_state()
        s.on_agent_msg("esp32_desk_light", "state", {"level": "DARK"})
        assert s.sensor_snapshot()["esp32_desk_light"]["state"] == {"level": "DARK"}

    def test_led_stored_in_actuators(self):
        s = make_state()
        s.on_agent_msg("esp32_desk_led", "state", {"ism": "BRIGHT"})
        assert s.actuator_snapshot()["esp32_desk_led"]["state"] == {"ism": "BRIGHT"}


class TestTaskResult:
    def test_store_and_retrieve(self):
        s = make_state()
        s.store_task_result("t1", {"result": "ok"})
        assert s.get_task_result("t1") == {"result": "ok"}

    def test_missing_returns_none(self):
        s = make_state()
        assert s.get_task_result("nonexistent") is None
