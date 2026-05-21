import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from unittest.mock import MagicMock
import queue
from desk_agent import DeskAgent


def make_agent(llm=None):
    return DeskAgent(
        shared_state=MagicMock(),
        publish_task_fn=MagicMock(),
        llm=llm or MagicMock(),
    )


class TestSkeleton:
    def test_instantiates(self):
        agent = make_agent()
        assert agent is not None

    def test_has_start_method(self):
        agent = make_agent()
        assert callable(agent.start)

    def test_has_push_sensor_event_method(self):
        agent = make_agent()
        assert callable(agent.push_sensor_event)

    def test_belief_history_starts_empty(self):
        agent = make_agent()
        assert agent._belief_history == []

    def test_cooldown_starts_empty(self):
        agent = make_agent()
        assert agent._cooldown == {}

    def test_push_sensor_event_puts_to_queue(self):
        agent = make_agent()
        agent.push_sensor_event("esp32_desk_light", {"level": "DARK"})
        assert agent._event_queue.qsize() == 1


class TestSense:
    def _make_agent_with_snapshot(self, snapshot: dict):
        state = MagicMock()
        state.sensor_snapshot.return_value = snapshot
        return DeskAgent(shared_state=state, publish_task_fn=MagicMock(), llm=MagicMock())

    def test_returns_none_when_no_light_data(self):
        agent = self._make_agent_with_snapshot({})
        assert agent._sense() is None

    def test_returns_light_level_dark(self):
        snap = {
            "esp32_desk_light": {
                "state": {"level": "DARK", "lux": 50, "ts": 1000},
            }
        }
        agent = self._make_agent_with_snapshot(snap)
        result = agent._sense()
        assert result is not None
        assert result["light_level"] == "DARK"
        assert result["light_lux"] == 50

    def test_falls_back_to_event_when_no_state(self):
        snap = {
            "esp32_desk_light": {
                "event": {"level": "DIM", "lux": 120, "ts": 1000},
            }
        }
        agent = self._make_agent_with_snapshot(snap)
        result = agent._sense()
        assert result["light_level"] == "DIM"

    def test_sound_recent_true_within_5s(self):
        now = time.time()
        snap = {
            "esp32_desk_light": {"state": {"level": "NORMAL", "lux": 300, "ts": now}},
            "esp32_desk_sound": {"event": {"ts": now - 2}},
        }
        agent = self._make_agent_with_snapshot(snap)
        result = agent._sense()
        assert result["sound_recent"] is True

    def test_sound_recent_false_older_than_5s(self):
        now = time.time()
        snap = {
            "esp32_desk_light": {"state": {"level": "NORMAL", "lux": 300, "ts": now}},
            "esp32_desk_sound": {"event": {"ts": now - 10}},
        }
        agent = self._make_agent_with_snapshot(snap)
        result = agent._sense()
        assert result["sound_recent"] is False

    def test_no_sound_sensor(self):
        snap = {
            "esp32_desk_light": {"state": {"level": "NORMAL", "lux": 300, "ts": 1000}},
        }
        agent = self._make_agent_with_snapshot(snap)
        result = agent._sense()
        assert result["sound_detected"] is False
        assert result["sound_recent"] is False


import time as _time
from langchain_core.messages import AIMessage


class TestReason:
    SENSE_DATA = {
        "light_level": "DARK",
        "light_lux": 30,
        "sound_detected": False,
        "sound_recent": False,
    }

    def _make_agent_with_llm_response(self, content: str):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content=content)
        return DeskAgent(shared_state=MagicMock(), publish_task_fn=None, llm=mock_llm)

    def test_returns_belief_dict(self):
        agent = self._make_agent_with_llm_response(
            '{"context": "光线昏暗", "space_mood": "昏暗", "should_act": true, '
            '"action": {"device": "esp32_desk_led", "cmd": "SET_STATE", "params": {"state": "BRIGHT"}}, '
            '"reason": "光线不足"}'
        )
        belief = agent._reason(self.SENSE_DATA)
        assert belief is not None
        assert belief["should_act"] is True
        assert belief["action"]["cmd"] == "SET_STATE"
        assert "ts" in belief

    def test_returns_none_on_invalid_json(self):
        agent = self._make_agent_with_llm_response("not valid json at all")
        belief = agent._reason(self.SENSE_DATA)
        assert belief is None

    def test_handles_json_with_preamble(self):
        agent = self._make_agent_with_llm_response(
            '好的，以下是结果：\n{"context": "正常", "space_mood": "空闲", '
            '"should_act": false, "action": {}, "reason": "ok"}'
        )
        belief = agent._reason(self.SENSE_DATA)
        assert belief is not None
        assert belief["should_act"] is False

    def test_prompt_includes_history_context(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(
            content='{"context": "test", "space_mood": "空闲", "should_act": false, "action": {}, "reason": "ok"}'
        )
        agent = DeskAgent(shared_state=MagicMock(), publish_task_fn=None, llm=mock_llm)
        agent._belief_history = [
            {"context": "空间安静", "space_mood": "空闲", "ts": _time.time() - 120},
            {"context": "有人进入", "space_mood": "专注", "ts": _time.time() - 60},
        ]
        agent._reason(self.SENSE_DATA)
        call_args = mock_llm.invoke.call_args[0][0]
        prompt_text = call_args[0].content
        assert "空间安静" in prompt_text
        assert "有人进入" in prompt_text
