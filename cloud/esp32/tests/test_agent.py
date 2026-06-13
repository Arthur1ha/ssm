import time
import pytest
from unittest.mock import MagicMock, patch
from langchain_core.messages import AIMessage
from cloud.esp32.agent import ESP32Agent
from cloud.esp32.state import ESP32State


def make_agent(llm=None):
    state = ESP32State()
    return ESP32Agent(state, llm=llm or MagicMock())


def make_agent_with_snapshot(sensor_snapshot, actuator_snapshot=None):
    mock_state = MagicMock(spec=ESP32State)
    mock_state.sensor_snapshot.return_value = sensor_snapshot
    mock_state.actuator_snapshot.return_value = actuator_snapshot or {}
    return ESP32Agent(mock_state, llm=MagicMock())


class TestSkeleton:
    def test_instantiates(self):
        assert make_agent() is not None

    def test_has_start_method(self):
        assert callable(make_agent().start)

    def test_has_push_sensor_event(self):
        assert callable(make_agent().push_sensor_event)

    def test_belief_history_starts_empty(self):
        assert make_agent()._belief_history == []

    def test_cooldown_starts_empty(self):
        assert make_agent()._cooldown == {}

    def test_push_sensor_event_puts_to_queue(self):
        agent = make_agent()
        agent.push_sensor_event("esp32_desk_light", {"level": "DARK"})
        assert agent._event_queue.qsize() == 1


class TestSense:
    def test_returns_none_when_no_light_data(self):
        agent = make_agent_with_snapshot({})
        assert agent._sense() is None

    def test_returns_light_level_dark(self):
        snap = {"esp32_desk_light": {"state": {"level": "DARK", "lux": 50, "ts": 1000}}}
        agent = make_agent_with_snapshot(snap)
        result = agent._sense()
        assert result is not None
        assert result["light_level"] == "DARK"
        assert result["light_lux"] == 50

    def test_falls_back_to_event_when_no_state(self):
        snap = {"esp32_desk_light": {"event": {"level": "DIM", "lux": 120, "ts": 1000}}}
        agent = make_agent_with_snapshot(snap)
        assert agent._sense()["light_level"] == "DIM"

    def test_sound_recent_true_within_5s(self):
        now = time.time()
        snap = {
            "esp32_desk_light": {"state": {"level": "NORMAL", "lux": 300, "ts": now}},
            "esp32_desk_sound": {"event": {"ts": now - 2}},
        }
        agent = make_agent_with_snapshot(snap)
        assert agent._sense()["sound_recent"] is True

    def test_sound_recent_false_older_than_5s(self):
        now = time.time()
        snap = {
            "esp32_desk_light": {"state": {"level": "NORMAL", "lux": 300, "ts": now}},
            "esp32_desk_sound": {"event": {"ts": now - 10}},
        }
        agent = make_agent_with_snapshot(snap)
        assert agent._sense()["sound_recent"] is False

    def test_no_sound_sensor(self):
        snap = {"esp32_desk_light": {"state": {"level": "NORMAL", "lux": 300, "ts": 1000}}}
        agent = make_agent_with_snapshot(snap)
        result = agent._sense()
        assert result["sound_detected"] is False
        assert result["sound_recent"] is False

    def test_no_context_combo_in_result(self):
        snap = {"esp32_desk_light": {"state": {"level": "DARK", "lux": 10, "ts": 1000}}}
        agent = make_agent_with_snapshot(snap)
        result = agent._sense()
        assert "context_combo" not in result

    def test_includes_led_device_id(self):
        snap = {"esp32_desk_light": {"state": {"level": "NORMAL", "lux": 300, "ts": 1000}}}
        mock_state = MagicMock(spec=ESP32State)
        mock_state.sensor_snapshot.return_value = snap
        mock_state.actuator_snapshot.return_value = {"esp32_desk_led": {"state": {"ism": "BRIGHT"}}}
        agent = ESP32Agent(mock_state, llm=MagicMock())
        result = agent._sense()
        assert result["led_device_id"] == "esp32_desk_led"


class TestReason:
    SENSE_DATA = {
        "light_level": "DARK", "light_value": 100, "light_lux": 30,
        "sound_detected": False, "sound_recent": False,
        "led_state": "OFF", "led_device_id": "esp32_desk_led",
        "time_str": "20:00", "time_period": "夜间",
    }

    def _make_with_llm_response(self, content: str):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content=content)
        return ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)

    def test_returns_list_of_tool_calls(self):
        agent = self._make_with_llm_response(
            '[{"tool": "set_led_color", "params": {"r": 255, "g": 160, "b": 60, "brightness": 160}}]'
        )
        result = agent._reason(self.SENSE_DATA)
        assert isinstance(result, list)
        assert result[0]["tool"] == "set_led_color"

    def test_returns_empty_list_for_no_action(self):
        agent = self._make_with_llm_response("[]")
        result = agent._reason(self.SENSE_DATA)
        assert result == []

    def test_returns_none_on_invalid_json(self):
        agent = self._make_with_llm_response("不是 JSON")
        assert agent._reason(self.SENSE_DATA) is None

    def test_handles_json_with_preamble(self):
        agent = self._make_with_llm_response(
            '好的，结果如下：\n[{"tool": "speak", "params": {"text": "光线不足"}}]'
        )
        result = agent._reason(self.SENSE_DATA)
        assert result is not None
        assert result[0]["tool"] == "speak"

    def test_prompt_includes_raw_sense_values(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content="[]")
        agent = ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)
        agent._reason(self.SENSE_DATA)
        prompt = mock_llm.invoke.call_args[0][0][0].content
        assert "DARK" in prompt
        assert "20:00" in prompt

    def test_prompt_includes_tool_descriptions(self):
        from cloud.esp32.tools import TOOL_DESCRIPTIONS
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content="[]")
        agent = ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)
        agent._reason(self.SENSE_DATA)
        prompt = mock_llm.invoke.call_args[0][0][0].content
        assert "set_led_state" in prompt or "set_led_color" in prompt

    def test_prompt_includes_history(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content="[]")
        agent = ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)
        agent._belief_history = [
            {"context": "光线昏暗有人活动", "actions": ["set_led_color"], "ts": time.time() - 120},
        ]
        agent._reason(self.SENSE_DATA)
        prompt = mock_llm.invoke.call_args[0][0][0].content
        assert "光线昏暗有人活动" in prompt

    def test_prompt_does_not_contain_explicit_rules(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content="[]")
        agent = ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)
        agent._reason(self.SENSE_DATA)
        prompt = mock_llm.invoke.call_args[0][0][0].content
        # 不应出现硬编码规则关键字
        assert "dark_active" not in prompt
        assert "→ state_action" not in prompt
        assert "决策规则（按优先级）" not in prompt


class TestExecute:
    def _make_agent(self):
        mock_state = MagicMock(spec=ESP32State)
        mock_state.actuator_snapshot.return_value = {
            "esp32_desk_led": {"state": {"ism": "OFF"}}
        }
        return ESP32Agent(mock_state, llm=MagicMock())

    def test_set_led_state_calls_tool(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.set_led_state") as mock_fn:
            agent._execute([{"tool": "set_led_state", "params": {"state": "BRIGHT"}}], "esp32_desk_led")
            mock_fn.assert_called_once_with(device_id="esp32_desk_led", state="BRIGHT")

    def test_set_led_color_calls_tool(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.set_led_color") as mock_fn:
            agent._execute(
                [{"tool": "set_led_color", "params": {"r": 255, "g": 100, "b": 50, "brightness": 180}}],
                "esp32_desk_led"
            )
            mock_fn.assert_called_once_with(device_id="esp32_desk_led", r=255, g=100, b=50, brightness=180)

    def test_speak_calls_tool(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.speak") as mock_fn:
            agent._execute([{"tool": "speak", "params": {"text": "灯已开启"}}], "esp32_desk_led")
            mock_fn.assert_called_once_with(text="灯已开启")

    def test_speak_does_not_inject_device_id(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.speak") as mock_fn:
            agent._execute([{"tool": "speak", "params": {"text": "你好"}}], "esp32_desk_led")
            call_kwargs = mock_fn.call_args[1]
            assert "device_id" not in call_kwargs

    def test_cooldown_blocks_duplicate_led_command(self):
        agent = self._make_agent()
        calls = [{"tool": "set_led_state", "params": {"state": "BRIGHT"}}]
        with patch("cloud.esp32.tools.set_led_state") as mock_fn:
            agent._execute(calls, "esp32_desk_led")
            agent._execute(calls, "esp32_desk_led")
            assert mock_fn.call_count == 1

    def test_cooldown_allows_different_state(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.set_led_state") as mock_fn:
            agent._execute([{"tool": "set_led_state", "params": {"state": "BRIGHT"}}], "esp32_desk_led")
            agent._execute([{"tool": "set_led_state", "params": {"state": "OFF"}}], "esp32_desk_led")
            assert mock_fn.call_count == 2

    def test_unknown_tool_skipped_without_exception(self):
        agent = self._make_agent()
        agent._execute([{"tool": "fly_to_moon", "params": {}}], "esp32_desk_led")

    def test_empty_list_does_nothing(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.set_led_state") as mock_fn:
            agent._execute([], "esp32_desk_led")
            mock_fn.assert_not_called()


class TestFindLedDevice:
    def test_finds_led_from_actuator_snapshot(self):
        mock_state = MagicMock(spec=ESP32State)
        mock_state.actuator_snapshot.return_value = {"esp32_desk_led": {}}
        agent = ESP32Agent(mock_state, llm=MagicMock())
        assert agent._find_led_device() == "esp32_desk_led"

    def test_fallback_when_no_led(self):
        mock_state = MagicMock(spec=ESP32State)
        mock_state.actuator_snapshot.return_value = {}
        agent = ESP32Agent(mock_state, llm=MagicMock())
        assert agent._find_led_device() == "esp32_desk_led"


class TestBeliefHistory:
    def test_reason_returns_list(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content="[]")
        agent = ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)
        sense = {
            "light_level": "NORMAL", "light_value": 300, "light_lux": 300,
            "sound_detected": False, "sound_recent": False,
            "led_state": "OFF", "led_device_id": "esp32_desk_led",
            "time_str": "10:00", "time_period": "上午",
        }
        result = agent._reason(sense)
        assert isinstance(result, list)


class TestAutonomyMode:
    def test_default_mode_is_reactive(self):
        assert make_agent().get_autonomy_mode() == "reactive"

    def test_set_mode_manual(self):
        a = make_agent()
        a.set_autonomy_mode("manual")
        assert a.get_autonomy_mode() == "manual"

    def test_set_invalid_mode_raises(self):
        a = make_agent()
        with pytest.raises(ValueError):
            a.set_autonomy_mode("bogus")

    def test_should_act_true_in_reactive(self):
        assert make_agent()._should_act() is True

    def test_should_act_false_in_manual(self):
        a = make_agent()
        a.set_autonomy_mode("manual")
        assert a._should_act() is False


class TestUserHold:
    def test_no_hold_by_default(self):
        assert make_agent()._in_user_hold() is False

    def test_mark_user_command_sets_hold(self):
        a = make_agent()
        a.mark_user_command("esp32_desk_led")
        assert a._in_user_hold() is True

    def test_should_act_false_during_hold(self):
        a = make_agent()
        a.mark_user_command("esp32_desk_led")
        assert a._should_act() is False

    def test_hold_expires(self):
        a = make_agent()
        a.mark_user_command("esp32_desk_led")
        a._user_hold_until = time.time() - 1   # 模拟窗口已过期
        assert a._in_user_hold() is False
        assert a._should_act() is True

