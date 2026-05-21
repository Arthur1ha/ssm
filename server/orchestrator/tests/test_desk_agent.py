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
