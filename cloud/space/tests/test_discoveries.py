import time

from cloud.space.activity import ActivityStore


def test_discoveries_are_scoped_fresh_bounded_and_evidence_only(tmp_path):
    store = ActivityStore(tmp_path / "activity.json")
    revision = store.note_user_intervention("user_event")
    activity = store.begin("我要工作了", "user_event", revision)
    activity_id = activity["activity_id"]
    store.set_device(activity_id, "lamp", mode="reactive", status="joined")
    store.set_device(activity_id, "dog", mode="free_explore", status="joined")
    assert store.current()["devices"]["lamp"]["joined_at"] >= activity["started_at"]
    store.activate(activity_id)
    own = store.record_event(activity_id, "observation", unit_id="lamp", text="灯自身观测")
    first = store.record_event(activity_id, "observation", unit_id="dog", text="看到一个人",
                               source="vision_description")
    latest = store.record_event(activity_id, "observation", unit_id="dog", text="看到一个人")
    identity = store.record_event(activity_id, "person_identified", unit_id="dog",
                                  text="已确认看到主人", source="identity_recognition", confidence=95)
    store.record_event(activity_id, "action_result", unit_id="dog", text="已受理", status="accepted")
    result = store.record_event(activity_id, "action_result", unit_id="dog", text="已完成", status="ok")
    store.record_event(activity_id, "message", unit_id="dog", text="来聊天吧")
    reply = store.record_event(activity_id, "message", unit_id="dog", text="我会保持安静",
                               reply_to="msg_one", conversation_id="conv_one")
    discoveries = store.recent_discoveries("lamp")
    assert {item["event_id"] for item in discoveries} == {
        latest["event_id"], identity["event_id"], result["event_id"], reply["event_id"]}
    assert own["event_id"] not in {item["event_id"] for item in discoveries}
    assert next(item for item in discoveries if item["event_type"] == "person_identified")["confidence"] == 95
    assert all(item["activity_id"] == activity_id for item in discoveries)
    assert len(store.recent_discoveries("lamp", limit=2)) == 2
    assert store.recent_discoveries("lamp", now=time.time() + 601) == []
    store.begin_end()
    assert store.recent_discoveries("lamp") == []
