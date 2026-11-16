"""Search visibility regressions with simulated observations; no physical robot."""
import asyncio
import time

import pytest

from cloud.agents.go2.workflows.search import DeviceSearch, build_search_graph
from cloud.agents.go2.workflows.search_progress import search_progress_facts
from cloud.observability import bind_context
from cloud.space.execution import ExecutionContext


@pytest.mark.asyncio
@pytest.mark.parametrize('captured_at', [1794820000.25, None])
async def test_search_observation_keeps_original_camera_time_not_monotonic_time(monkeypatch, captured_at):
    from unittest.mock import AsyncMock
    from cloud.go2.perception import visual_target
    from cloud.go2.connection import go2

    search = object.__new__(DeviceSearch)
    search.guard = AsyncMock(return_value=None)
    search.after_frame = search.after_time = None
    monkeypatch.setattr(go2, '_velocity_command_active', False)
    frame = {'frame_id': 'original-frame', 'received_at': 123.0, 'captured_at': captured_at}
    monkeypatch.setattr(visual_target, 'observe_target', AsyncMock(return_value={
        'found': True, 'frame': frame, 'pose': None,
        'observation': {'scene_summary': '前方有纸箱。'}}))
    observation = await search.observe({'mode': 'find_object', 'target': '纸箱',
                                       'deadline': time.time() + 10})
    facts = search_progress_facts('search_observe', {}, observation=observation)
    assert observation['image_time'] == 123.0
    assert facts['observation']['as_of'] == captured_at
    assert facts['observation']['frame_id'] == 'original-frame'


def test_visible_facts_keep_sighting_arrival_and_absence_distinct():
    state = {"target": "白色猫头鹰"}
    observation = {"found": True, "frame_id": "view-one", "observation": {
        "scene_summary": "白色猫头鹰位于椅子左侧。", "proximity": "far",
        "proximity_reason": "目标与相机之间仍有一段地面。"}}
    facts = search_progress_facts("search_candidate", state, observation=observation)
    assert facts["text"] == "白色猫头鹰位于椅子左侧。"
    assert facts["observation"]["proximity_reason"] == "目标与相机之间仍有一段地面。"
    assert facts["observation"]["found"] is True
    assert facts["observation"]["proximity"] == "far"
    assert "已到" not in facts["text"]
    assert facts["observation"]["frame_id"] == "view-one"
    missing = search_progress_facts("search_observe", state, observation={
        "found": False, "observation": {"scene_summary": "前方有桌腿和纸箱。"}})
    assert "桌腿和纸箱" in missing["text"]
    assert missing["observation"]["found"] is False


def test_progress_bounds_model_text_and_tolerates_older_observations():
    facts = search_progress_facts("search_observe", {}, observation={
        "observation": {"scene_summary": "长" * 1000, "proximity_reason": {"invalid": True}}})
    assert len(facts["observation"]["scene_summary"]) == 240
    assert facts["observation"]["proximity_reason"] == ""
    unknown = search_progress_facts("search_observe", {})
    assert unknown["observation"]["found"] is None
    assert unknown["text"] == ""


@pytest.mark.asyncio
async def test_activity_search_steps_and_photos_are_journaled_once(tmp_path, monkeypatch):
    from cloud.api import intents
    from cloud.space.activity import ActivityStore

    store = ActivityStore(tmp_path / "activity.json")
    revision = store.note_user_intervention("start-search")
    activity = store.begin("一起找猫头鹰", "start-search", revision)
    activity_id = activity["activity_id"]
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: store)
    monkeypatch.setattr("cloud.go2.router.get_activity_mode_owner", lambda: activity_id)
    broker = intents._EventBroker()
    monkeypatch.setattr(intents, "_events", broker)
    channel = broker.subscribe("viewer")
    emitted = []
    store.configure(emit=emitted.append)
    search = object.__new__(DeviceSearch)
    search.context = ExecutionContext(activity_id, "visitor", False, origin_kind="agent_autonomous")
    search.task_id = "search-activity"
    search.progress_sequence = 0
    state = {"mode": "find_object", "target": "猫头鹰"}
    await search.progress("search_observing", state)
    await search.progress("search_observe", state, observation={
        "media_ref": "/api/search-photo", "observation": {"scene_summary": "前方有纸箱。"}})
    await search.progress("search_selected", state, candidate={"source": "frontier"})

    rows = [row for row in store.events(activity_id) if row["event_type"] == "search_progress"]
    assert [row["stage"] for row in rows] == ["search_observing", "search_observe", "search_selected"]
    assert all(row["task_id"] == search.task_id for row in rows)
    assert rows[1]["media_ref"] == "/api/search-photo"
    assert "纸箱" in rows[1]["text"]
    assert emitted == rows
    assert ActivityStore(tmp_path / "activity.json").events(activity_id)[-3:] == rows
    # A replay refreshes neither the journal nor its photo count.
    replay = channel.get_nowait()
    assert replay["activity_id"] == activity_id
    intents.publish_agent_event(**{key: value for key, value in replay.items()
                                  if key not in {"type", "ts"} and value is not None},
                                event_type="agent_progress")
    assert len(store.events(activity_id)) == 4
    store.begin_end()
    store.finish_end(activity_id)
    reopened = ActivityStore(tmp_path / "activity.json")
    assert [row for row in reopened.events(activity_id) if row["event_type"] == "search_progress"] == rows


@pytest.mark.asyncio
async def test_late_search_progress_does_not_join_a_new_activity(tmp_path, monkeypatch):
    from cloud.api import intents
    from cloud.space.activity import ActivityStore

    store = ActivityStore(tmp_path / "activity.json")
    revision = store.note_user_intervention("new-activity")
    current = store.begin("新的活动", "new-activity", revision)["activity_id"]
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: store)
    monkeypatch.setattr("cloud.go2.router.get_activity_mode_owner", lambda: current)
    broker = intents._EventBroker()
    monkeypatch.setattr(intents, "_events", broker)
    channel = broker.subscribe("viewer")
    search = object.__new__(DeviceSearch)
    search.context = ExecutionContext("act_previous", "visitor", False, origin_kind="agent_autonomous")
    search.task_id, search.progress_sequence = "old-search", 0
    await search.progress("search_observe", {"target": "猫头鹰"})
    assert len(store.events(current)) == 1
    assert channel.get_nowait()["activity_id"] == "act_previous"


@pytest.mark.asyncio
async def test_observation_start_and_result_reach_sse_before_slow_operations(monkeypatch):
    from cloud.api import intents
    broker = intents._EventBroker()
    monkeypatch.setattr(intents, "_events", broker)
    channel = broker.subscribe("viewer")
    other = broker.subscribe("other-viewer")
    captured = asyncio.Event()
    release_capture = asyncio.Event()
    recording = asyncio.Event()
    release_record = asyncio.Event()

    class Replay(DeviceSearch):
        def __init__(self):
            self.context = ExecutionContext("viewer", "visitor", False)
            self.task_id = "search-test"
            self.progress_sequence = 0

        async def guard(self, state):
            return None

        async def observe(self, state):
            captured.set()
            await release_capture.wait()
            return {"found": False, "frame_id": "view-one", "media_ref": "/api/search-photo",
                    "observation": {"scene_summary": "前方有椅子和纸箱。"}}

        async def record(self, observation):
            recording.set()
            await release_record.wait()
            return {"observations": 1}

        async def select(self, state):
            return {"reason": "coverage_exhausted"}

    with bind_context(trace_id="tr_test", parent_run_id="steward-test"):
        work = asyncio.create_task(build_search_graph(Replay()).ainvoke({
            "task_id": "search-test", "mode": "find_object", "target": "猫头鹰",
            "phase": "observe", "deadline": time.time() + 30, "steps": 0}))
    try:
        await asyncio.wait_for(captured.wait(), 2)
        started = channel.get_nowait()
        assert started["stage"] == "search_observing"
        assert started["fact_kind"] == "call"
        assert started["tool"] == "go2_observe"
        assert not work.done()
        release_capture.set()
        await asyncio.wait_for(recording.wait(), 2)
        observed = channel.get_nowait()
        assert observed["stage"] == "search_observe"
        assert "椅子和纸箱" in observed["text"]
        assert observed["media_ref"] == "/api/search-photo"
        assert observed["trace_id"] == "tr_test"
        assert observed["parent_run_id"] == "steward-test"
        assert observed["run_id"] == observed["task_id"] == "search-test"
        assert observed["event_id"] != started["event_id"]
        assert intents._sse_event_name(observed) == "agent_progress"
        assert other.empty()
        assert not work.done(), "Observation facts must arrive before map/coverage work finishes"
        release_record.set()
        await asyncio.wait_for(work, 2)
        selecting = channel.get_nowait()
        assert selecting["stage"] == "search_selecting"
        assert selecting.get("media_ref") is None, "Do not repeat the previous photo as a new observation"
    finally:
        release_capture.set()
        release_record.set()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.parametrize("source", [
    "target_reobservation", "frontier", "observation_reposition", "visual_coverage",
])
def test_selected_candidate_keeps_executor_facts_without_invented_model_reason(source):
    facts = search_progress_facts("search_selected", {}, candidate={"source": source})
    assert facts["public_result"]["candidate"]["source"] == source
    assert facts["text"] == ""
    assert "decision_basis" not in facts


def test_turn_and_blocked_movement_do_not_claim_approach_or_completion():
    turning = {"candidate": {"source": "target_reobservation", "view_kind": "turn"}}
    facts = search_progress_facts("search_navigate", turning)
    assert facts["fact_kind"] == "call"
    assert facts["text"] == ""
    blocked = search_progress_facts("search_move_result", turning, receipt={
        "result": "blocked", "reason": "observation_turn_unconfirmed"})
    assert blocked["reason"] == "observation_turn_unconfirmed"
    assert blocked["public_result"]["result"] == "blocked"
    assert "已完成" not in blocked["text"]


@pytest.mark.asyncio
async def test_vision_requests_scene_facts_in_the_same_inference(monkeypatch):
    from cloud.go2.perception import visual_target
    calls = []

    async def ask(*args):
        calls.append(args)
        return {"found": False, "scene_summary": "前方有纸箱。"}

    monkeypatch.setattr(visual_target, "_ask_vision", ask)
    result = await visual_target.detect_visible_target(b"image", "猫头鹰", {"frame_id": "view-one"})
    assert len(calls) == 1
    assert "scene_summary" in calls[0][3]
    assert "无论是否找到目标都必须提供" in calls[0][3]
    assert result["scene_summary"] == "前方有纸箱。"
