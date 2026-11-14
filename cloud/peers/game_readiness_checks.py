"""Bidirectional invitations require both live device owners to be available."""

import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cloud.go2.control import ControlLease
from cloud.go2.state import Go2State
from cloud.peers.games import get_game, playable_games
from cloud.peers.games.base import game_invitation_ready
from cloud.peers.messaging import AgentMessenger
from cloud.peers.social import SocialState

LAMP, DOG = "esp32_desk_led", "go2"


@pytest.fixture
def idle(monkeypatch):
    activity = {"activity_id": "act_idle", "status": "active", "started_at": time.time(),
                "devices": {unit: {"status": "joined"} for unit in (LAMP, DOG)}}
    store = SimpleNamespace(current=lambda: activity, record_event=Mock(return_value={}))
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: store)
    lamp = SimpleNamespace(_should_act=lambda: True, _device_online=lambda: True,
                           is_busy_for_game=lambda: False)
    monkeypatch.setattr("cloud.esp32.service.get_agent", lambda: lamp)
    runtime = Go2State()
    runtime.connection("online")
    robot = SimpleNamespace(is_connected=True, runtime=runtime)
    monkeypatch.setattr("cloud.go2.connection.go2", robot)
    drive = SimpleNamespace(user_interrupt=False, _pending_question_id=None, _active_goal=None)
    monkeypatch.setattr("cloud.go2.mode.free_explore.drive", drive)
    lease = ControlLease()
    monkeypatch.setattr("cloud.go2.control.control_lease", lease)
    return SimpleNamespace(activity=activity, lamp=lamp, robot=robot, runtime=runtime,
                           drive=drive, lease=lease)


def test_registered_games_accept_either_invitation_direction(idle):
    for game_id in ("color_hunt", "push_delivery", "rps_siege"):
        game = get_game(game_id)
        assert game.allows_pair(LAMP, DOG)
        assert game.allows_pair(DOG, LAMP)
        assert not game.allows_pair(DOG, DOG)
        assert not game.allows_pair("other", LAMP)
    for sender, target in ((LAMP, DOG), (DOG, LAMP)):
        assert game_invitation_ready("act_idle", sender, target)
        assert [row["game_id"] for row in playable_games("act_idle", sender, target)] == ["color_hunt", "push_delivery", "rps_siege"]


@pytest.mark.parametrize("changes", [
    {"life_state": "play", "life_task_id": "task"},
    {"life_state": "explore", "life_task_id": "search"},
    {"life_state": None}, {"activity_state": "rest"},
    {"pending_action": {"command": "Hello"}}, {"navigation_active": True},
    {"velocity_requested": True}, {"control_mode": "remote"},
    {"body_state": "moving"}, {"connected": False},
])
def test_busy_dog_prevents_either_side_from_inviting(idle, changes):
    idle.runtime.commit("test_busy", **changes)
    for sender, target in ((LAMP, DOG), (DOG, LAMP)):
        assert not game_invitation_ready("act_idle", sender, target)
        assert playable_games("act_idle", sender, target) == []


def test_exploration_between_tasks_can_invite_but_live_goal_cannot(idle):
    idle.runtime.commit("exploring", life_state="explore", life_task_id=None)
    assert game_invitation_ready("act_idle", DOG, LAMP)
    idle.drive._active_goal = {"task_id": "looking"}
    assert not game_invitation_ready("act_idle", DOG, LAMP)


@pytest.mark.parametrize("field,value", [
    ("user_interrupt", True), ("_pending_question_id", "question"),
])
def test_interrupted_or_waiting_dog_cannot_invite(idle, field, value):
    setattr(idle.drive, field, value)
    assert not game_invitation_ready("act_idle", DOG, LAMP)


def test_missing_or_busy_lamp_cannot_be_treated_as_idle(idle, monkeypatch):
    idle.lamp.is_busy_for_game = lambda: True
    assert not game_invitation_ready("act_idle", DOG, LAMP)
    idle.lamp.is_busy_for_game = lambda: False
    idle.lamp._should_act = lambda: False
    assert not game_invitation_ready("act_idle", LAMP, DOG)
    monkeypatch.setattr("cloud.esp32.service.get_agent", lambda: None)
    assert not game_invitation_ready("act_idle", DOG, LAMP)


@pytest.mark.parametrize("sender,target", [(LAMP, DOG), (DOG, LAMP)])
def test_signed_invitation_keeps_direction_and_blocks_crossed_invite(idle, monkeypatch, sender, target):
    monkeypatch.setenv("SSM_MQTT_SIGNING_SECRET", "idle-game-test-signing-secret")
    sent, delivered = [], []
    mqtt = SimpleNamespace(publish=lambda topic, body, **_kw:
                           sent.append((topic, body)) or SimpleNamespace(rc=0))
    messenger = AgentMessenger(mqtt, None, None, {LAMP, DOG}, delivered.append)
    messenger._allowed = lambda *_: True
    messenger._resolve_decision = lambda *_: {"reason": "both idle", "evidence": []}
    try:
        result = messenger.send_message(sender, target,
            "一起玩找颜色吧？你亮颜色我来找，找到了就算我赢。" if sender == DOG else
            "一起玩找颜色吧？我亮颜色你来找，找到了就算你赢。",
            interaction_kind="game_invite", game_id="color_hunt")
        assert result["result"] == "accepted"
        payload = json.loads(sent[-1][1])
        assert (payload["sender"], payload["target"]) == (sender, target)
        assert messenger.has_pending_game(DOG)
        assert messenger.has_pending_game(LAMP)
        assert not messenger.has_pending_game("other")
        crossed = messenger.send_message(target, sender, "也来玩找颜色吧",
            interaction_kind="game_invite", game_id="color_hunt")
        assert crossed == {"result": "blocked", "error": "game_invitation_pending"}
        assert len(sent) == 1
        messenger.handle_message(*sent[0])
        messenger._workers.submit(lambda: None).result()
        assert len(delivered) == 1
        assert delivered[0]["message_id"] == result["message_id"]
    finally:
        messenger.close()


def test_new_task_after_invitation_prevents_delivery_to_acceptance(idle, monkeypatch):
    monkeypatch.setenv("SSM_MQTT_SIGNING_SECRET", "idle-game-test-signing-secret")
    sent, delivered = [], []
    mqtt = SimpleNamespace(publish=lambda topic, body, **_kw:
                           sent.append((topic, body)) or SimpleNamespace(rc=0))
    messenger = AgentMessenger(mqtt, None, None, {LAMP, DOG}, delivered.append)
    messenger._allowed = lambda *_: True
    messenger._resolve_decision = lambda *_: {"reason": "both idle", "evidence": []}
    try:
        result = messenger.send_message(DOG, LAMP, "你亮一种颜色，我去找同色物品，找到了就算我赢。",
            interaction_kind="game_invite", game_id="color_hunt")
        assert result["result"] == "accepted"
        idle.lamp.is_busy_for_game = lambda: True
        messenger.handle_message(*sent[0])
        messenger._workers.submit(lambda: None).result()
        assert delivered == []
    finally:
        messenger.close()


def test_idle_opportunity_is_event_driven_and_not_a_periodic_invitation(idle):
    social = SocialState()
    peers = [{"unit_id": DOG}]
    chance = social.opportunity(idle.activity, peers, [], idle_ready=True)
    assert chance["kind"] == "idle"
    social.evaluated(chance)
    # Remaining idle, even much later, cannot force recurring invitations.
    assert social.opportunity(idle.activity, peers, [], idle_ready=True,
                              now=time.time() + 3600) is None
    assert social.opportunity(idle.activity, peers, [], idle_ready=False) is None
    next_chance = social.opportunity(idle.activity, peers, [], idle_ready=True)
    assert next_chance["kind"] == "idle"
    assert next_chance["event_ids"] != chance["event_ids"]
