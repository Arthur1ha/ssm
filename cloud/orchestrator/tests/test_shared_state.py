"""cloud.orchestrator.tests.test_shared_state — SharedState 分桶逻辑。

回归 #5：执行器按 manifest agent_type 判定，不再硬编码 unit_id 后缀 "led"。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_state import SharedState   # noqa: E402


def test_actuator_judged_by_manifest_not_led_suffix():
    """buzzer 是 actuator 但后缀不是 led，应按 manifest agent_type 进 actuator 桶。"""
    s = SharedState()
    s.on_manifest("esp32_desk_buzzer", {"unit_id": "esp32_desk_buzzer", "agent_type": "actuator"})
    s.on_agent_msg("esp32_desk_buzzer", "state", {"ism": "OFF"})
    assert "esp32_desk_buzzer" in s.actuator_snapshot()
    assert "esp32_desk_buzzer" not in s.sensor_snapshot()


def test_sensor_without_manifest_defaults_to_sensor():
    """无 manifest 的 unit 默认进 sensor 桶（agent_type 未知不当执行器）。"""
    s = SharedState()
    s.on_agent_msg("esp32_desk_light", "event", {"level": "DIM"})
    assert "esp32_desk_light" in s.sensor_snapshot()
    assert "esp32_desk_light" not in s.actuator_snapshot()
