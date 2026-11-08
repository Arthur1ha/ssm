"""Activity evidence preserves facts without inventing actions or prompt history."""

import json
from types import SimpleNamespace

from cloud.space.activity import ActivityStore


def start(tmp_path):
    store = ActivityStore(tmp_path / "activity.json")
    revision = store.note_user_intervention("evt_user")
    record = store.begin("设备自己玩吧", "evt_user", revision)
    return store, record["activity_id"]


def test_action_feedback_is_bounded_and_preserved_across_reload(tmp_path, monkeypatch):
    state = {"ism": "ON", "r": 255, "g": 200, "b": 80, "brightness": 120,
             "_received_at": 100, "private_memory": "do not store", "video": "do not store"}
    registry = SimpleNamespace(get_card=lambda _unit: {"state": state})
    monkeypatch.setattr("cloud.cards.registry.get_registry", lambda: registry)
    monkeypatch.setattr("cloud.space.activity.time.time", lambda: 101)
    store, activity_id = start(tmp_path)
    planned = store.record_event(activity_id, "action", unit_id="lamp", task_id="task_one",
                                 action="set_led_color", trigger_message_id="msg_invite",
                                 decision_observation={"light_level": "DARK", "sound_detected": False,
                                                       "time_period": "夜间", "prompt": "do not store"})
    state.update(r=0, g=0, b=255, _received_at=102)
    monkeypatch.setattr("cloud.space.activity.time.time", lambda: 103)
    result = store.record_event(activity_id, "action_result", unit_id="lamp", task_id="task_one",
                               action="set_led_color", status="ok")
    assert planned["state_at_event"]["values"]["r"] == 255
    assert result["state_at_event"]["values"]["b"] == 255
    assert planned["trigger_message_id"] == "msg_invite"
    assert planned["decision_observation"] == {"light_level": "DARK", "sound_detected": False, "time_period": "夜间"}
    assert "do not store" not in json.dumps(store.events(activity_id))
    loaded = ActivityStore(tmp_path / "activity.json")
    assert loaded.events(activity_id)[-1]["state_at_event"] == result["state_at_event"]


def test_old_or_missing_feedback_remains_unconfirmed(tmp_path, monkeypatch):
    state = {"ism": "ON", "ts": 1}
    monkeypatch.setattr("cloud.cards.registry.get_registry",
                        lambda: SimpleNamespace(get_card=lambda _unit: {"state": state}))
    monkeypatch.setattr("cloud.space.activity.time.time", lambda: 100)
    store, activity_id = start(tmp_path)
    stale = store.record_event(activity_id, "action_result", unit_id="lamp", status="timeout")
    assert stale["state_at_event"]["status"] == "stale"
    assert stale["status"] == "timeout"
    state.clear()
    missing = store.record_event(activity_id, "action", unit_id="lamp")
    assert missing["state_at_event"] == {"status": "unknown"}


def test_dialogue_does_not_gain_state_or_trigger_evidence(tmp_path):
    store, activity_id = start(tmp_path)
    event = store.record_event(activity_id, "message", unit_id="go2", text="我想玩一会儿")
    assert "state_at_event" not in event
    assert "trigger_message_id" not in event
    assert "decision_observation" not in event
    store.begin_end()
    store.finish_end(activity_id)
    assert store.record_event(activity_id, "action", unit_id="go2") is None


def test_lamp_progress_keeps_one_physical_task_and_explicit_message_link(monkeypatch):
    from cloud.api import intents
    from cloud.esp32.physical_tasks import stable_task_id

    recorded = []
    store = SimpleNamespace(current=lambda _space: {"activity_id": "act_test"},
                            record_event=lambda *_args, **facts: recorded.append(facts))
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: store)
    monkeypatch.setattr("cloud.esp32.router.get_activity_mode_owner", lambda: "act_test")
    monkeypatch.setattr(intents, "_events", SimpleNamespace(emit_space=lambda *_args: None))
    for stage, status in (("decision", "decision"), ("dispatching", "executing"), ("observed", "ok")):
        intents.publish_agent_event("agent_progress", agent_id="esp32_desk_led", scope="space",
                                    source="autonomous", status=status, run_id="msg_one", step_id="call_one",
                                    stage=stage, tool="set_led_color", trigger_message_id="msg_one")
    assert len(recorded) == 3
    assert {row["task_id"] for row in recorded} == {stable_task_id("esp32_desk_led", "msg_one", "call_one")}
    assert all(row["trigger_message_id"] == "msg_one" for row in recorded)
