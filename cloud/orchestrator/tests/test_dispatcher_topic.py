"""cloud.orchestrator.tests.test_dispatcher_topic — Dispatcher 必须用 unit_id 拼 task topic。

回归 #1：slug（如 desk-lamp）绝不能进 MQTT topic，否则 ESP32 收不到任务。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import graph as graph_mod   # noqa: E402
import tools as _t          # noqa: E402


def test_dispatcher_uses_unit_id_not_slug(monkeypatch):
    """LED card 的 slug=desk-lamp、unit_id=esp32_desk_led，派发必须用 unit_id。"""
    card = {
        "slug": "desk-lamp",
        "unit_id": "esp32_desk_led",
        "transport": {"kind": "mqtt", "task_topic": "ssm/task/esp32_desk_led/{task_id}"},
        "skills": [{"id": "set_light_state", "invoke": {"action": "SET_STATE"}}],
    }
    registry = MagicMock()
    registry.get_card.return_value = card
    monkeypatch.setattr(_t, "_registry", registry)
    monkeypatch.setattr(_t, "do_publish_feedback", lambda *a, **k: None)

    captured = {}

    def fake_publish_task(unit_id, task_id, action, params, session_id):
        captured["unit_id"] = unit_id

    monkeypatch.setattr(_t, "do_publish_task", fake_publish_task)

    node = graph_mod._make_dispatcher_node()
    state = {
        "session_id": "s1", "early_exit": False,
        "planned_tasks": [{"slug": "desk-lamp", "skill_id": "set_light_state",
                           "task_id": "s1_t0", "params": {"state": "BRIGHT"}}],
        "task_results": {},
    }
    node(state)
    assert captured["unit_id"] == "esp32_desk_led"


def test_dispatcher_errors_when_unit_id_missing(monkeypatch):
    """mqtt card 缺 unit_id 时必须报错，绝不退回用 slug 拼 topic。"""
    card = {"slug": "desk-lamp", "transport": {"kind": "mqtt"},
            "skills": [{"id": "set_light_state", "invoke": {"action": "SET_STATE"}}]}
    registry = MagicMock()
    registry.get_card.return_value = card
    monkeypatch.setattr(_t, "_registry", registry)
    monkeypatch.setattr(_t, "do_publish_feedback", lambda *a, **k: None)

    called = {"n": 0}
    monkeypatch.setattr(_t, "do_publish_task",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    node = graph_mod._make_dispatcher_node()
    out = node({
        "session_id": "s1", "early_exit": False,
        "planned_tasks": [{"slug": "desk-lamp", "skill_id": "set_light_state",
                           "task_id": "s1_t0", "params": {"state": "BRIGHT"}}],
        "task_results": {},
    })
    assert called["n"] == 0
    assert out["task_results"]["s1_t0"]["error"] == "missing_unit_id"
