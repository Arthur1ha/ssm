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
