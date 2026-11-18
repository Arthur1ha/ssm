"""Focused offline boundaries: private attempts, closed games and fresh invites."""
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
import time

import pytest

from cloud.peers.games.scope import (
    begin_game_close, game_scope, game_scope_available,
    terminal_game_scope, terminal_scope_available,
)
from cloud.peers.messaging import AgentMessenger
from cloud.peers.social import SocialState, activity_peers
from cloud.peers.turns import peer_turn_available
from cloud.space.activity import ActivityStore
from cloud.space.context import get_space_context

LAMP, DOG = "esp32_desk_led", "go2"


@pytest.fixture
def activity(tmp_path, monkeypatch):
    from cloud.space import activity as module
    store = ActivityStore(tmp_path / "activity.json")
    monkeypatch.setattr(module, "_store", store)
    monkeypatch.setattr(module, "_state_at_event", lambda *_: {})
    revision = store.note_user_intervention("request")
    record = store.begin("你们玩送货游戏", "request", revision)
    aid = record["activity_id"]
    for unit in (LAMP, DOG):
        store.set_device(aid, unit, mode="reactive" if unit == LAMP else "free_explore", status="joined")
    store.activate(aid)
    return store, aid


def test_attempts_stay_visible_but_only_completed_subtasks_enter_shared_context(activity):
    store, aid = activity
    social = SocialState()
    now = time.time()
    with game_scope(aid, "invitation", "push_delivery"):
        attempt = store.record_event(aid, "action_result", unit_id=DOG,
                                     text="control_error", status="blocked", run_id="courier")
        result = store.record_event(aid, "task_result", unit_id=DOG,
            text="已经确认这件货物。", status="ok", run_id="courier",
            source_event_id="task:result:courier", context_scope="shared")
        store.record_event(aid, "task_result", unit_id=DOG,
            text="已经确认这件货物。", status="ok", run_id="courier",
            source_event_id="task:result:courier", context_scope="shared")
    assert attempt["context_scope"] == "process"
    assert len([row for row in store.events(aid) if row["event_type"] == "task_result"]) == 1
    discoveries = store.recent_discoveries(LAMP, now=now + 1)
    assert [row["event_id"] for row in discoveries] == [result["event_id"]]
    assert [row["event_id"] for row in store.shared_events(aid)] == [result["event_id"]]
    # A raw internal failure cannot itself schedule a new social turn.
    assert social.opportunity(store.current(), [{"unit_id": DOG}], [attempt], now=now + 1) is None


def test_closing_revokes_effects_and_dialogue_but_keeps_late_receipts(activity):
    store, aid = activity
    with game_scope(aid, "invitation", "push_delivery"):
        assert game_scope_available()
        begin_game_close()
        assert not game_scope_available() and not peer_turn_available()
        assert store.record_event(aid, "message", unit_id=LAMP, text="再推一次") is None
        late = store.record_event(aid, "action_result", unit_id=DOG,
                                  status="ok", text="原任务实际停稳", task_id="original")
        assert late["task_id"] == "original"
    with terminal_game_scope(aid, "invitation", "push_delivery"):
        assert terminal_scope_available() and not peer_turn_available()
        assert store.record_event(aid, "message", unit_id=LAMP,
            stage="game_final", text="这局到这里啦。", context_scope="process")
        assert store.record_event(aid, "message", unit_id=LAMP, text="再来一局") is None
    store.record_event(aid, "game_ended", game_id="push_delivery", game_instance_id="invitation")
    with terminal_game_scope(aid, "invitation", "push_delivery"):
        assert not terminal_scope_available()
    assert not store.shared_events(aid)
    assert store.game_allows(aid, "new_invitation")


def test_free_activity_context_drops_old_dialogue_and_consumes_original_goal(activity):
    store, aid = activity
    with game_scope(aid, "invitation", "push_delivery"):
        store.record_event(aid, "message", unit_id=LAMP, text="继续推到蓝点")
        store.record_event(aid, "task_result", unit_id=DOG, text="本单未能完成", status="blocked")
        begin_game_close()
    store.record_event(aid, "game_ended", game_id="push_delivery", game_instance_id="invitation")
    observation = store.record_event(aid, "observation", unit_id=LAMP, text="新的环境变化")
    registry = SimpleNamespace(get_card=lambda _: None)
    context = get_space_context(DOG, state=SimpleNamespace(), registry=registry,
                                constraints=SimpleNamespace(list_for=lambda *a, **kw: []))
    assert context["activity"]["phase"] == "free_activity"
    assert context["activity"]["request_consumed"]
    assert context["activity"]["initial_request"]["text"] == "你们玩送货游戏"
    assert context["activity"]["context_source"] == "runtime"
    assert context["interaction_history"]["events"] == []
    assert [row["event_id"] for row in context["recent_discoveries"]] == [observation["event_id"]]
    model_activity, _ = activity_peers(DOG, [{"unit_id": LAMP, "online": True}])
    assert model_activity["context"] == context["activity"]["context"]


def test_closed_invitation_cannot_continue_but_a_new_signed_invite_can_start(activity, monkeypatch):
    store, aid = activity
    monkeypatch.setenv("SSM_MQTT_SIGNING_SECRET", "offline-game-scope-signing-secret")
    from cloud.peers import games
    from cloud.peers.games import base
    monkeypatch.setattr(games, "game_is_playable", lambda *_: True)
    monkeypatch.setattr(base, "game_invitation_ready", lambda *_: True)
    published = []
    mqtt = SimpleNamespace(publish=lambda topic, raw, **kw:
                           published.append((topic, raw)) or SimpleNamespace(rc=0))
    bus = AgentMessenger(mqtt, None, None, {LAMP, DOG}, Mock())
    bus._allowed = lambda *_: True
    bus._resolve_decision = lambda *_: {"reason": "本次自主邀请", "evidence": []}
    try:
        first = bus.send_message(LAMP, DOG, "来玩送货吗？", interaction_kind="game_invite",
                                 game_id="push_delivery", decision_reason="本次自主邀请", evidence_refs=[])
        assert first["result"] == "accepted"
        assert bus.handle_message(*published[-1])
        bus._workers.submit(lambda: None).result()
        invitation = first["message_id"]
        with game_scope(aid, invitation, "push_delivery"):
            begin_game_close()
        assert bus.send_message(DOG, LAMP, "我再试一次", reply_to=invitation)["error"] == "activity_ended"
        bus.close_game(invitation, result={"result": "ok", "reason": "completed"}, task_id="courier_task")
        assert bus.get_status(invitation)["result"]["result"] == "ok"
        second = bus.send_message(LAMP, DOG, "重新邀请一局。", interaction_kind="game_invite",
                                  game_id="push_delivery", decision_reason="新的自主邀请", evidence_refs=[])
        assert second["result"] == "accepted" and second["message_id"] != invitation
        assert bus.get_status(second["message_id"])["game_instance_id"] == second["message_id"]
    finally:
        bus.close()


def test_retained_snapshot_carries_closed_game_without_replaying_local_history(activity, tmp_path):
    store, aid = activity
    snapshots = []
    store.configure(publish=snapshots.append)
    with game_scope(aid, "invitation", "push_delivery"):
        begin_game_close()
    store.record_event(aid, "game_ended", game_id="push_delivery", game_instance_id="invitation")
    snapshot = snapshots[-1]
    assert snapshot["activity"]["phase"] == "free_activity"
    follower = ActivityStore(tmp_path / "follower.json")
    follower._current = snapshot["activity"]
    follower._events[aid] = [{"event_type": "message", "text": "only local history"}]
    assert follower.current()["request_consumed"]
    assert not follower.game_allows(aid, "invitation")


def test_game_modules_cannot_introduce_another_model_or_device_execution_loop():
    """Future registered games must use the same device-brain adapter."""
    import ast
    from pathlib import Path
    directory = Path(__file__).parent / "games"
    for path in directory.glob("*.py"):
        if path.name.endswith("_checks.py"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {
                    "cloud.agents.common.models", "cloud.go2.models",
                    "cloud.go2.tools.executor", "cloud.esp32.tool_executor"}, path
            if isinstance(node, ast.Call):
                assert not (isinstance(node.func, ast.Attribute)
                            and node.func.attr in {"with_structured_output", "bind_tools"}), path


@pytest.mark.asyncio
async def test_game_owner_closes_after_two_final_lines_and_discards_old_queue(activity, monkeypatch):
    from cloud.go2.mode import free_explore
    from cloud.peers import games
    from cloud.peers.games import base, role_graph
    from cloud.space.execution import ExecutionContext
    store, aid = activity
    drive = free_explore.Drive()
    drive.owner_session_id = aid
    drive._context = ExecutionContext(aid, "visitor", False,
        origin_kind="agent_autonomous", expires_at=int(time.time() + 60))
    drive._queue_completed_game = Mock()
    monkeypatch.setattr(free_explore, "go2", SimpleNamespace(is_connected=True))
    monkeypatch.setattr(free_explore, "runtime_for", lambda _: None)
    monkeypatch.setattr(base, "game_invitation_ready", lambda *_: True)
    monkeypatch.setattr(games, "game_is_playable", lambda *_: True)
    monkeypatch.setattr("cloud.agents.common.effects.FileEffectLedger",
                        lambda: SimpleNamespace(reserve=lambda *a, **kw: {"reservation": "new"}))
    async def execute(*_args):
        store.record_event(aid, "game_started", game_id="push_delivery", trigger_message_id="original")
        begin_game_close()
        return {"result": "ok", "reason": "directional_push_attempt_completed"}
    game = SimpleNamespace(game_id="push_delivery", execute=execute, metadata=lambda: {})
    monkeypatch.setattr(games, "get_game", lambda _: game)
    finals = AsyncMock(return_value=[
        {"role": "lamp", "speech": "这局到这里。"},
        {"role": "go2", "speech": "我已经停稳了。"}])
    monkeypatch.setattr(role_graph, "final_responses", finals)
    messenger = Mock()
    messenger.get_received_result.return_value = None
    drive._events.extend([
        {"kind": "peer_message", "message_id": "late", "game_instance_id": "original", "messenger": messenger},
        {"kind": "social", "event_id": "unrelated"}])
    message = {"message_id": "accepted", "sender": LAMP, "target": DOG,
        "activity_id": aid, "game_instance_id": "original", "conversation_id": "original"}
    await drive._handle_event_owned({"event_id": "accepted", "message_id": "accepted",
        "kind": "peer_message", "source": LAMP, "activity_id": aid,
        "interaction_kind": "game_invite", "game_id": "push_delivery",
        "original_invitation_id": "original", "game_preaccepted": True,
        "message": message, "messenger": messenger, "deadline": time.time() + 50,
        "decision": {"choice": "accept"}}, None, None, "test")
    rows = store.events(aid)
    final_rows = [row for row in rows if row.get("stage") == "game_final"]
    assert [row["unit_id"] for row in final_rows] == [LAMP, DOG]
    assert rows[-1]["event_type"] == "game_ended" and rows[-1]["status"] == "ok"
    assert not store.game_allows(aid, "original")
    assert [event["event_id"] for event in drive._events] == ["unrelated"]
    assert drive._active_goal is None
    messenger.close_game.assert_called_once_with("original",
        result={"result": "ok", "reason": "directional_push_attempt_completed"}, task_id="peer_accepted")
    messenger.send_result.assert_called_once_with("late", "cancelled", reason="game_ended")
    drive._queue_completed_game.assert_called_once()
