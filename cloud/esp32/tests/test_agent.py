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


class TestReason:
    SENSE_DATA = {
        "light_level": "DARK", "light_value": 100, "light_lux": 30,
        "sound_detected": False, "sound_recent": False,
        "led_state": "OFF", "time_str": "20:00", "time_period": "夜间",
        "context_combo": "dark_silent",
    }

    def _make_with_llm_response(self, content: str):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content=content)
        return ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)

    def test_returns_belief_dict(self):
        agent = self._make_with_llm_response(
            '{"context": "光线昏暗", "space_mood": "昏暗", "should_act": true, '
            '"state_action": {"state": "BRIGHT"}, "color_action": null, '
            '"reason": "光线不足", "speech_text": "", "thought_text": "", '
            '"should_verbalize_thought": false, "proactive_report": false}'
        )
        belief = agent._reason(self.SENSE_DATA)
        assert belief is not None
        assert belief["should_act"] is True
        assert "ts" in belief

    def test_returns_none_on_invalid_json(self):
        agent = self._make_with_llm_response("not valid json at all")
        assert agent._reason(self.SENSE_DATA) is None

    def test_handles_json_with_preamble(self):
        agent = self._make_with_llm_response(
            '好的，结果如下：\n{"context": "正常", "space_mood": "空闲", "should_act": false, '
            '"state_action": null, "color_action": null, "reason": "ok", '
            '"speech_text": "", "thought_text": "", "should_verbalize_thought": false, "proactive_report": false}'
        )
        belief = agent._reason(self.SENSE_DATA)
        assert belief is not None
        assert belief["should_act"] is False

    def test_prompt_includes_history(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(
            content='{"context": "test", "space_mood": "空闲", "should_act": false, '
            '"state_action": null, "color_action": null, "reason": "ok", '
            '"speech_text": "", "thought_text": "", "should_verbalize_thought": false, "proactive_report": false}'
        )
        agent = ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)
        agent._belief_history = [
            {"context": "空间安静", "ts": time.time() - 120},
            {"context": "有人进入", "ts": time.time() - 60},
        ]
        agent._reason(self.SENSE_DATA)
        prompt = mock_llm.invoke.call_args[0][0][0].content
        assert "空间安静" in prompt
        assert "有人进入" in prompt


class TestAct:
    def _make_agent(self):
        mock_state = MagicMock(spec=ESP32State)
        mock_state.actuator_snapshot.return_value = {
            "esp32_desk_led": {"state": {"ism": "BRIGHT"}}
        }
        return ESP32Agent(mock_state, llm=MagicMock())

    def _belief_state(self, state="BRIGHT", color=None):
        return {
            "state_action": {"state": state},
            "color_action": color,
        }

    def _belief_color(self, r=255, g=200, b=100, brightness=180):
        return {
            "state_action": None,
            "color_action": {"r": r, "g": g, "b": b, "brightness": brightness},
        }

    def test_publishes_set_state_bright(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.publish_task") as mock_pub:
            agent._act(self._belief_state("BRIGHT"))
            mock_pub.assert_called_once()
            assert mock_pub.call_args[0][2] == "SET_STATE"

    def test_bright_with_color_uses_set_color(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.publish_task") as mock_pub:
            agent._act({"state_action": {"state": "BRIGHT"}, "color_action": {"r": 255, "g": 100, "b": 50, "brightness": 180}})
            assert mock_pub.call_args[0][2] == "SET_COLOR"

    def test_off_ignores_color(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.publish_task") as mock_pub:
            agent._act({"state_action": {"state": "OFF"}, "color_action": {"r": 255, "g": 0, "b": 0, "brightness": 255}})
            assert mock_pub.call_args[0][2] == "SET_STATE"
            assert mock_pub.call_args[0][3] == {"state": "OFF"}

    def test_null_actions_publish_nothing(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.publish_task") as mock_pub:
            agent._act({"state_action": None, "color_action": None})
            mock_pub.assert_not_called()

    def test_cooldown_blocks_duplicate(self):
        agent = self._make_agent()
        belief = self._belief_state("BRIGHT")
        with patch("cloud.esp32.tools.publish_task") as mock_pub:
            agent._act(belief)
            agent._act(belief)
            assert mock_pub.call_count == 1

    def test_cooldown_allows_different_state(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.publish_task") as mock_pub:
            agent._act(self._belief_state("BRIGHT"))
            agent._act(self._belief_state("OFF"))
            assert mock_pub.call_count == 2


class TestBeliefHistory:
    def test_history_capped_at_10(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(
            content='{"context": "x", "space_mood": "空闲", "should_act": false, '
            '"state_action": null, "color_action": null, "reason": "ok", '
            '"speech_text": "", "thought_text": "", "should_verbalize_thought": false, "proactive_report": false}'
        )
        agent = ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)
        sense = {
            "light_level": "NORMAL", "light_value": 300, "light_lux": 300,
            "sound_detected": False, "sound_recent": False,
            "led_state": "OFF", "time_str": "10:00", "time_period": "上午",
            "context_combo": "normal_silent",
        }
        for _ in range(15):
            b = agent._reason(sense)
            if b:
                agent._belief_history.append(b)
                if len(agent._belief_history) > 10:
                    agent._belief_history.pop(0)
        assert len(agent._belief_history) == 10
