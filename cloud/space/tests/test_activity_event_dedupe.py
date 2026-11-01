"""A replayed Graph reply adds one activity timeline item."""

from cloud.space.activity import ActivityStore


def test_activity_event_replay_uses_source_event_id(tmp_path):
    path = tmp_path / "activity.json"
    store = ActivityStore(path)
    store._current = {"activity_id": "act-1", "status": "active"}
    first = store.record_event("act-1", "message", unit_id="go2",
                               text="汪，招呼打完啦。", status="ok",
                               source_event_id="evt-graph-reply")
    reopened = ActivityStore(path)
    repeated = reopened.record_event("act-1", "message", unit_id="go2",
                                      text="汪，招呼打完啦。", status="ok",
                                      source_event_id="evt-graph-reply")
    assert repeated == first
    assert len(reopened.events("act-1")) == 1

