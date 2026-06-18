"""灯自主循环命中调教 → 执行 → 计数。"""
import json
from cloud.esp32.agent import ESP32Agent
from cloud.esp32.state import ESP32State
from cloud.esp32.memory import taught


class _StubLLM:
    def __init__(self, payload):
        self._payload = payload

    def invoke(self, _messages):
        class _Resp:
            content = json.dumps(self._payload, ensure_ascii=False)
        return _Resp()


def test_reason_includes_taught_and_execute_counts(tmp_path, monkeypatch):
    f = tmp_path / "taught.json"
    monkeypatch.setattr(taught, "TAUGHT_FILE", f)
    rule = taught.add("天黑了", "调亮一点",
                      action_hint={"tool": "set_led_state"}, path=f)

    # LLM 返回一个带 taught_id 的工具调用
    agent = ESP32Agent(ESP32State(), llm=_StubLLM(
        [{"tool": "set_led_state", "params": {"state": "BRIGHT"},
          "taught_id": rule["id"]}]))

    sense = {"light_level": "DARK", "light_value": 5, "light_lux": 5,
             "sound_detected": False, "led_state": "OFF",
             "led_device_id": "esp32_desk_led",
             "time_str": "20:00", "time_period": "夜间", "proactive_hints": []}

    calls = agent._reason(sense)
    assert any(c.get("taught_id") == rule["id"] for c in calls)

    # 执行（stub 掉真正下发，只验证计数）
    monkeypatch.setattr(agent, "_dispatch_tool", lambda *a, **k: True)
    agent._execute(calls, "esp32_desk_led")

    assert taught.list_all(path=f)[0]["hit_count"] == 1
