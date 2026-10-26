"""Browser task lookup stays on the original physical ID and session."""

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from cloud.api import intents, main


def test_lamp_task_lookup_is_session_scoped_and_result_aware(monkeypatch):
    task_id = "task_" + "a" * 32
    session = {"session_id": "owner"}
    state = MagicMock()
    monkeypatch.setattr(main, "_esp32_state", state)
    with intents._lamp_task_owners_lock:
        intents._lamp_task_owners[task_id] = ("esp32_desk_led", "owner")
    try:
        state.get_task_result.return_value = None
        pending = asyncio.run(intents.get_lamp_command_task("esp32_desk_led", task_id, session))
        assert pending == {"result": "executing", "task_id": task_id}
        intents.record_lamp_task_result(task_id, {"result": "ok", "task_id": task_id})
        state.get_task_result.return_value = None  # state ring may have evicted it
        done = asyncio.run(intents.get_lamp_command_task("esp32_desk_led", task_id, session))
        assert done["result"] == "ok"
        with pytest.raises(HTTPException) as exc:
            asyncio.run(intents.get_lamp_command_task("esp32_desk_led", task_id,
                                                      {"session_id": "other"}))
        assert exc.value.status_code == 404
        state.get_task_result.assert_called_once_with(task_id)
    finally:
        with intents._lamp_task_owners_lock:
            intents._lamp_task_owners.pop(task_id, None)
            intents._lamp_task_results.pop(task_id, None)
