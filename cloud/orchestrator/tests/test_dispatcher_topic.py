"""cloud.orchestrator.tests.test_dispatcher_topic — Dispatcher 用 unit_id 拼 task topic。

回归 #1：mqtt 派发必须用 unit_id 作为 topic 段，ESP32 才能收到任务。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import graph as graph_mod   # noqa: E402
import tools as _t          # noqa: E402


def test_dispatcher_publishes_to_unit_id_topic(monkeypatch):
    """mqtt task 派发用 task['unit_id'] 作为 do_publish_task 的 topic 段。"""
    card = {
        "unit_id": "esp32_desk_led",
        "transport": {"kind": "mqtt"},
        "skills": [{"id": "set_light_state", "invoke": {"action": "SET_STATE"}}],
    }
    registry = MagicMock()
    registry.get_card.return_value = card
    monkeypatch.setattr(_t, "_registry", registry)
    monkeypatch.setattr(_t, "do_publish_feedback", lambda *a, **k: None)

    captured = {}
    monkeypatch.setattr(_t, "do_publish_task",
                        lambda unit_id, *a, **k: captured.__setitem__("unit_id", unit_id))

    node = graph_mod._make_dispatcher_node()
    node({
        "session_id": "s1", "early_exit": False,
        "planned_tasks": [{"unit_id": "esp32_desk_led", "skill_id": "set_light_state",
                           "task_id": "s1_t0", "params": {"state": "BRIGHT"}}],
        "task_results": {},
    })
    assert captured["unit_id"] == "esp32_desk_led"
