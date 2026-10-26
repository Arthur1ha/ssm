"""A device agent receives only its own delegated task as its goal."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import multitask


def test_go2_motion_does_not_receive_unrelated_lamp_goal(monkeypatch):
    skill = {"id": "go2_sport", "params_schema": {}}
    monkeypatch.setattr(multitask, "_cards", lambda _context: {
        "go2": {"online": True, "skills": [skill]}})
    monkeypatch.setattr(multitask, "validate_public_skill", lambda *_args: {
        "kind": "http", "endpoint": "/api/go2/chat"})
    captured = {}

    def dispatch(_endpoint, body, _timeout):
        captured.update(body)
        return {"result": "ok"}

    monkeypatch.setattr(multitask.transport, "do_http_dispatch", dispatch)
    context = {"session_id": "session", "actor": "admin", "persist": True,
               "space_id": "main", "user_msg": "把灯关掉，然后让狗坐下"}
    result = multitask._invoke_skill(context, {
        "unit_id": "go2", "skill_id": "go2_sport", "params": {"cmd": "StandDown"}},
        "go2-step", time.time() + 30)

    assert result["result"] == "ok"
    assert "StandDown" in captured["message"]
    assert "灯" not in captured["message"]
