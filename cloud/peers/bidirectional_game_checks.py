"""Offline reverse invitations preserve willingness, physical evidence and IDs."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cloud.peers.games import color_hunt

LAMP, DOG = "esp32_desk_led", "go2"


@pytest.fixture
def reverse(monkeypatch, tmp_path):
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    message = {"kind": "message", "interaction_kind": "game_invite", "game_id": "color_hunt",
               "message_id": "invite_dog", "conversation_id": "invite_dog",
               "activity_id": "act_reverse", "sender": DOG, "target": LAMP,
               "content": "你来亮颜色，我找同色物品，想玩吗？", "deadline": time.time() + 180}
    messenger = Mock(spec=["send_result", "send_message", "send_progress", "get_status"])
    messenger.send_message.return_value = {"result": "accepted", "message_id": "lamp_accepts"}
    monkeypatch.setattr("cloud.peers.games.base.game_invitation_ready", lambda *_: True)
    monkeypatch.setattr(color_hunt, "activity_allows", lambda *_: True)
    monkeypatch.setattr("cloud.esp32.service.get_agent", lambda: SimpleNamespace())
    decision = Mock(return_value={"event_decision": "accept", "decision_reason": "我想出个颜色题，看看你能找到什么。"})
    monkeypatch.setattr(color_hunt, "_lamp_invitation_decision", decision)

    def setup(recorder, _message, _state):
        recorder.send_result(_message["message_id"], "ok", task_id="lamp_physical_task")

    prepare = Mock(side_effect=setup)
    monkeypatch.setattr(color_hunt, "_prepare_lamp_color", prepare)
    return SimpleNamespace(message=message, messenger=messenger, decision=decision, prepare=prepare)


def test_lamp_prepares_real_color_before_accepting_dog_invitation(reverse):
    sequence = []
    setup = reverse.prepare.side_effect
    reverse.prepare.side_effect = lambda *args: (sequence.append("physical_setup"), setup(*args))[-1]
    reverse.messenger.send_message.side_effect = lambda *_args, **_kw: (
        sequence.append("accepted"), {"result": "accepted", "message_id": "lamp_accepts"})[-1]
    color_hunt._handle_color_hunt_invitation(reverse.messenger, reverse.message, None)
    assert sequence == ["physical_setup", "accepted"]
    args, kwargs = reverse.messenger.send_message.call_args
    assert args == (LAMP, DOG, reverse.decision.return_value["decision_reason"])
    assert kwargs == {"reply_to": "invite_dog", "reply_kind": "accepted", "deadline": reverse.message["deadline"]}
    reverse.messenger.send_result.assert_not_called()  # Preparation is not game completion.


@pytest.mark.parametrize("choice", ["decline", "defer"])
def test_lamp_can_decline_or_defer_without_lighting_action(reverse, choice):
    reverse.decision.return_value = {"event_decision": choice, "decision_reason": "我现在想先休息一下。"}
    color_hunt._handle_color_hunt_invitation(reverse.messenger, reverse.message, None)
    reverse.prepare.assert_not_called()
    assert reverse.messenger.send_message.call_args.args == (LAMP, DOG, "我现在想先休息一下。")
    assert reverse.messenger.send_message.call_args.kwargs["reply_kind"] == "declined"


def test_old_invitation_never_runs_lamp_graph_or_setup(reverse):
    reverse.message["deadline"] = time.time() - 1
    color_hunt._handle_color_hunt_invitation(reverse.messenger, reverse.message, None)
    reverse.decision.assert_not_called()
    reverse.prepare.assert_not_called()
    reverse.messenger.send_message.assert_not_called()


def test_replayed_invitation_cannot_repeat_lamp_preparation(reverse):
    color_hunt._handle_color_hunt_invitation(reverse.messenger, reverse.message, None)
    color_hunt._handle_color_hunt_invitation(reverse.messenger, reverse.message, None)
    reverse.prepare.assert_called_once()
    reverse.messenger.send_message.assert_called_once()
    assert reverse.messenger.send_result.call_args.kwargs["reason"] == "prior_game_execution_unconfirmed"


def test_task_claimed_during_preparation_prevents_acceptance(reverse, monkeypatch):
    readiness = Mock(side_effect=[True, True, False])
    monkeypatch.setattr("cloud.peers.games.base.game_invitation_ready", readiness)
    color_hunt._handle_color_hunt_invitation(reverse.messenger, reverse.message, None)
    reverse.prepare.assert_called_once()
    reverse.messenger.send_message.assert_not_called()
    assert reverse.messenger.send_result.call_args.args[1] == "cancelled"


@pytest.fixture
def prepared(reverse, monkeypatch):
    original = {**reverse.message, "state": "waiting_start"}
    accepted = {"kind": "message", "message_id": "lamp_accepts", "conversation_id": "invite_dog",
                "reply_to": "invite_dog", "reply_kind": "accepted", "activity_id": "act_reverse",
                "sender": LAMP, "target": DOG, "content": "我来出题。", "deadline": original["deadline"]}
    color = {"rgb": (255, 0, 0), "brightness": 180, "as_of": time.time()}
    event = {"event_type": "action_result", "game_id": "color_hunt", "trigger_message_id": "invite_dog",
             "action": "set_led_color", "status": "ok", "task_id": "lamp_physical_task", "ts": time.time(),
             "state_at_event": {"status": "current", "values": {"r": 255, "g": 0, "b": 0, "brightness": 180}}}
    store = SimpleNamespace(events=lambda _activity: [event])
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: store)
    physical = Mock(return_value={"task_id": "lamp_physical_task", "result": "ok"})
    monkeypatch.setattr("cloud.esp32.physical_tasks.get", physical)
    monkeypatch.setattr(color_hunt, "live_lamp_color", lambda _unit: color)
    drive = SimpleNamespace(submit_accepted_game=Mock(return_value=True))
    monkeypatch.setattr("cloud.go2.mode.free_explore.drive", drive)
    return SimpleNamespace(**vars(reverse), original=original, accepted=accepted,
                           color=color, event=event, physical=physical, drive=drive)


def test_reverse_acceptance_queues_dog_only_with_bound_real_preparation(prepared):
    color_hunt._queue_reverse_color_hunt(prepared.messenger, prepared.accepted, prepared.original)
    prepared.physical.assert_called_once_with("lamp_physical_task", unit_id=LAMP,
                                              session_id="peer_game_color_invite_dog")
    prepared.drive.submit_accepted_game.assert_called_once_with(
        prepared.accepted, prepared.original, prepared.messenger, prepared.color)


@pytest.mark.parametrize("failure", ["color_changed", "edge_unknown", "old_event", "wrong_invitation", "expired"])
def test_reverse_acceptance_rejects_missing_or_changed_physical_evidence(prepared, failure):
    if failure == "color_changed":
        prepared.color["rgb"] = (0, 255, 0)
    elif failure == "edge_unknown":
        prepared.physical.return_value = {"result": "unknown"}
    elif failure == "old_event":
        prepared.event["ts"] = time.time() - 61
    elif failure == "wrong_invitation":
        prepared.event["trigger_message_id"] = "another_invite"
    else:
        prepared.original["deadline"] = time.time() - 1
    color_hunt._queue_reverse_color_hunt(prepared.messenger, prepared.accepted, prepared.original)
    prepared.drive.submit_accepted_game.assert_not_called()
    assert prepared.messenger.send_result.call_args.args[1] == "rejected"


def test_final_reverse_result_is_relayed_to_original_invitation(prepared):
    from cloud.peers.inbound import handle_api_peer_message
    accepted_status = {**prepared.accepted, "state": "ok", "task_id": "peer_lamp_accepts",
                       "result": {"result": "ok", "reason": "candidate_reached"}}
    original = {**prepared.original, "state": "waiting_result"}
    prepared.messenger.get_status.side_effect = lambda identifier: {
        "lamp_accepts": accepted_status, "invite_dog": original}.get(identifier)
    result = {"kind": "result", "message_id": "lamp_accepts", "sender": DOG, "target": LAMP}
    handle_api_peer_message(prepared.messenger, result, None)
    prepared.messenger.send_result.assert_called_once_with(
        "invite_dog", "ok", task_id="peer_lamp_accepts", reason="candidate_reached")


def test_preaccepted_execution_is_reserved_by_original_invitation(reverse, monkeypatch):
    from cloud.go2.mode import free_explore
    from cloud.peers.games import Game
    execute = AsyncMock()
    game = Game("color_hunt", "找颜色", "规则", (LAMP, DOG), lambda *_: True, execute, bidirectional=True)
    monkeypatch.setattr("cloud.peers.games.get_game", lambda _id: game)
    monkeypatch.setattr("cloud.peers.games.game_is_playable", lambda *_: True)
    monkeypatch.setattr(free_explore, "runtime_for", lambda _robot: None)
    monkeypatch.setattr("cloud.peers.games.activity_allows", lambda *_: True)
    drive = free_explore.Drive()
    drive._activity_id = lambda: "act_reverse"
    event = {"kind": "peer_message", "message_id": "accepted_1", "event_id": "accepted_1",
             "activity_id": "act_reverse", "source": LAMP, "deadline": time.time() + 120,
             "interaction_kind": "game_invite", "game_id": "color_hunt", "game_preaccepted": True,
             "original_invitation_id": "invite_dog", "messenger": reverse.messenger,
             "message": {"sender": LAMP, "target": DOG}, "decision": {"choice": "accept"}}
    asyncio.run(drive._handle_event_owned(event, None, None, None))
    asyncio.run(drive._handle_event_owned({**event, "event_id": "accepted_2", "message_id": "accepted_2"}, None, None, None))
    execute.assert_awaited_once()
    reverse.messenger.send_message.assert_not_called()  # No second invitation or acceptance.
    assert reverse.messenger.send_result.call_args.kwargs["reason"] == "prior_game_execution_unconfirmed"


def test_social_invitation_is_considered_after_exploration_releases_task(monkeypatch):
    from cloud.go2.mode import free_explore
    drive = free_explore.Drive()
    runtime = SimpleNamespace(active_reason=lambda: None, exploration_enabled=True, life_task_available=True,
                              begin_life=Mock(return_value="life_token"), busy=True)
    runtime.finish_life = lambda _token: setattr(runtime, "busy", False)
    monkeypatch.setattr(free_explore, "runtime_for", lambda _robot: runtime)
    monkeypatch.setattr(free_explore, "go2", SimpleNamespace(is_connected=True))
    observation = {"event_id": "fresh_observation", "text": "红色玩偶"}
    drive._do_explore_owned = AsyncMock(return_value=observation)

    async def evaluate(actual):
        assert actual is observation
        assert runtime.busy is False

    drive._evaluate_social = AsyncMock(side_effect=evaluate)
    asyncio.run(drive._do_explore())
    drive._evaluate_social.assert_awaited_once_with(observation)


def test_shared_arbitration_uses_lamps_persona_and_context():
    from cloud.agents.go2.workflows.free_explore import build_motivation_graph
    prompts = []

    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, messages):
            prompts.extend(messages)
            return {"choice": "decline", "reason": "我想先安静一会。"}

    graph = build_motivation_graph(profile_loader=lambda: {"traits": {"social_initiative": 12}},
                                   model=Model(), role_name="灯")
    result = asyncio.run(graph.ainvoke({"connected": True, "pending_event": {
        "kind": "peer_message", "game": color_hunt.GAME.metadata()}}))
    assert result["event_decision"] == "decline"
    assert "你是 灯" in prompts[0].content
    assert "social_initiative" in prompts[1].content
