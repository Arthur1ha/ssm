"""Activity membership reaches the lamp worker without dispatching a game."""

import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cloud.api import activity
from cloud.agents.lamp.workflows.runtime import ReactiveMind
from cloud.space.activity import ActivityStore


@pytest.mark.parametrize("lamp_status", ["joined", "offline", "unavailable"])
async def test_lamp_wakes_only_after_joined_membership_is_committed(
        tmp_path, monkeypatch, lamp_status):
    store = ActivityStore(tmp_path / "activity.json")
    revision = store.note_user_intervention("evt_test")
    snapshots = []
    notify = Mock(side_effect=lambda _id: snapshots.append(store.current()))
    lamp = SimpleNamespace(_sense=lambda: {"light_available": True},
                           on_activity_started=notify)
    monkeypatch.setattr(activity, "get_activity_store", lambda: store)
    monkeypatch.setattr("cloud.esp32.service.get_agent", lambda: lamp)
    monkeypatch.setenv("ESP32_LED_UNIT_ID", "esp32_desk_led")
    monkeypatch.setattr(activity, "_activity_cards", lambda: [
        {"unit_id": "go2", "online": True},
        {"unit_id": "esp32_desk_led", "online": lamp_status != "offline"},
    ])
    monkeypatch.setattr(activity, "_selected_mode", lambda card:
                        "free_explore" if card["unit_id"] == "go2" else "reactive")

    async def switch(unit_id, _mode, _activity_id):
        assert notify.call_count == 0
        if unit_id == "esp32_desk_led" and lamp_status == "unavailable":
            raise RuntimeError("unavailable")
        return {"ok": True}

    monkeypatch.setattr(activity, "_switch_mode", AsyncMock(side_effect=switch))
    result = await activity.start_activity("你两玩石头剪刀布", "evt_test", revision)
    assert result["devices"]["esp32_desk_led"]["status"] == lamp_status
    assert notify.call_count == (1 if lamp_status == "joined" else 0)
    if snapshots:
        assert snapshots[0] == result
        assert snapshots[0]["status"] == "active"
        assert all(member["status"] == "joined" for member in snapshots[0]["devices"].values())


def test_lamp_activity_wakeup_is_a_scheduling_signal():
    pending = queue.Queue()
    lamp = SimpleNamespace(_event_queue=pending, _autonomy_epoch=7,
                           _should_act=lambda: True)
    ReactiveMind.on_activity_started(lamp, "act_new")
    signal = pending.get_nowait()
    assert signal["kind"] == "activity_started"
    assert signal["activity_id"] == "act_new"
    assert signal["autonomy_epoch"] == 7
    assert signal["eligible_at_arrival"] is True
    assert "payload" not in signal and "unit_id" not in signal
