"""Space activity lifecycle with simulated devices and no broker or hardware."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from cloud.api import activity as api
from cloud.space.activity import ActivityStore


def test_activity_cards_reads_registry_values(monkeypatch):
    card = {"unit_id": "esp32_desk_led", "device_id": "esp32_desk", "online": True,
            "modes": [{"id": "autonomy", "options": [{"value": "reactive"}]}]}
    monkeypatch.setattr(api, "get_registry", lambda: SimpleNamespace(
        get_all_cards=lambda: {card["unit_id"]: card}))
    monkeypatch.setattr(api, "get_space_registry", lambda: SimpleNamespace(
        get_space=lambda: {"device_ids": ["esp32_desk"]}))
    assert api._activity_cards() == [card]


def test_card_discovery_failure_does_not_leave_starting_activity(tmp_path, monkeypatch):
    store = ActivityStore(tmp_path / "activity.json")
    monkeypatch.setattr(api, "get_activity_store", lambda: store)

    def unavailable_cards():
        raise RuntimeError("discovery_failed")

    monkeypatch.setattr(api, "_activity_cards", unavailable_cards)

    async def scenario():
        source = "evt_" + "f" * 32
        revision = await api.user_intervention(source)
        with pytest.raises(RuntimeError, match="discovery_failed"):
            await api.start_activity("我要工作了", source, revision, "ses_a")
        assert store.current() is None
        assert store.get(store.latest_id())["end_reason"] == "start_failed"

    asyncio.run(scenario())


def test_activity_is_open_ended_partial_and_ends_before_next_request(tmp_path, monkeypatch):
    store = ActivityStore(tmp_path / "activity.json")
    published = []
    visible = []
    store.configure(publish=published.append, emit=visible.append)
    monkeypatch.setattr(api, "get_activity_store", lambda: store)
    monkeypatch.setattr(api, "_activity_cards", lambda: [
        {"unit_id": "esp32_desk_led", "online": True,
         "modes": [{"id": "autonomy", "options": [{"value": "reactive"}]}]},
        {"unit_id": "go2", "online": False,
         "modes": [{"id": "autonomy", "options": [{"value": "free_explore"}]}]},
    ])
    switched = []

    async def switch(unit_id, mode, activity_id):
        switched.append((unit_id, mode, activity_id))
        return {"ok": True, "mode": mode, "physical_stop": "confirmed"}

    monkeypatch.setattr(api, "_switch_mode", switch)

    async def scenario():
        source = "evt_" + "a" * 32
        revision = await api.user_intervention(source)
        activity = await api.start_activity("我要学习了", source, revision, "ses_a")
        assert activity["status"] == "partial"
        assert "expires_at" not in activity
        assert activity["devices"]["esp32_desk_led"]["status"] == "joined"
        assert activity["devices"]["go2"]["status"] == "offline"
        assert store.record_event(activity["activity_id"], "message", unit_id="esp32_desk_led",
                                  text="我来照亮桌面", message_id="msg_1")
        await api.user_intervention("evt_" + "b" * 32)
        assert store.current() is None
        assert store.record_event(activity["activity_id"], "observation", text="late") is None
        assert switched[-1] == ("esp32_desk_led", "manual", activity["activity_id"])
        assert store.get(activity["activity_id"])["devices"]["esp32_desk_led"]["physical_stop"] == "confirmed"
        assert published[-1]["activity"] is None
        assert [item["event_type"] for item in visible] == [
            "started", "device_joined", "device_left", "message", "device_left", "ended"]
        with pytest.raises(HTTPException) as stale:
            await api.start_activity("旧消息", source, revision, "ses_a")
        assert stale.value.status_code == 409

    asyncio.run(scenario())


def test_store_keeps_key_events_when_detail_is_trimmed(tmp_path):
    store = ActivityStore(tmp_path / "activity.json")
    store.note_user_intervention("evt_" + "c" * 32)
    activity = store.begin("我要睡了", "evt_" + "c" * 32, 1)
    activity_id = activity["activity_id"]
    for number in range(120):
        store.record_event(activity_id, "observation", text=str(number))
    store.begin_end()
    store.finish_end(activity_id)
    events = store.events(activity_id)
    assert [item["event_type"] for item in events].count("observation") == 100
    assert events[0]["event_type"] == "started"
    assert events[-1]["event_type"] == "ended"
    assert ActivityStore(tmp_path / "activity.json").events(activity_id) == events


def test_restart_finishes_interrupted_ending_state(tmp_path, monkeypatch):
    path = tmp_path / "activity.json"
    store = ActivityStore(path)
    store.note_user_intervention("evt_" + "d" * 32)
    activity_id = store.begin("我要出门了", "evt_" + "d" * 32, 1)["activity_id"]
    store.begin_end()
    recovered = ActivityStore(path)
    published = []
    recovered.configure(publish=published.append)
    monkeypatch.setattr(api, "get_activity_store", lambda: recovered)
    asyncio.run(api.reconcile_current_activity())
    assert recovered.current() is None
    assert recovered.get(activity_id)["status"] == "ended"
    assert published[-1]["activity"] is None


def test_restart_finishes_interrupted_starting_state(tmp_path, monkeypatch):
    path = tmp_path / "activity.json"
    store = ActivityStore(path)
    source = "evt_" + "e" * 32
    store.note_user_intervention(source)
    activity_id = store.begin("我要工作了", source, 1)["activity_id"]
    recovered = ActivityStore(path)
    monkeypatch.setattr(api, "get_activity_store", lambda: recovered)
    asyncio.run(api.reconcile_current_activity())
    assert recovered.current() is None
    assert recovered.get(activity_id)["status"] == "ended"
