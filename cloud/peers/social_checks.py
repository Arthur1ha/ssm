"""Social opportunities are event driven, without timing or topic quotas."""

import pytest

from cloud.peers.social import SocialState


@pytest.mark.parametrize("score", [0, 50, 100])
def test_fresh_events_allow_immediate_same_topic_sends(score):
    social = SocialState()
    now = social._born_at + 1
    activity = {"activity_id": "act_social", "started_at": now}
    peers = [{"unit_id": "go2"}]
    assert not social.can_send("go2", "再玩一局", now=now)
    for index in range(6):
        discovery = {"event_id": f"evt_{index}", "activity_id": "act_social",
                     "event_type": "observation", "ts": now}
        opportunity = social.opportunity(activity, peers, [discovery], score, now=now)
        assert opportunity is not None
        assert social.can_send("go2", "再玩一局", now=now)
        social.sent("go2", "再玩一局", now=now)
        social.evaluated(opportunity, now=now)
        assert social.opportunity(activity, peers, [discovery], score, now=now) is None
    assert social.opportunity(activity, peers, [], score, now=now + 3600) is None
    social.reset(None)
    assert not social.can_send("go2", "再玩一局", now=now)


def test_messages_stale_or_other_activity_observations_do_not_create_opportunities():
    social = SocialState()
    now = social._born_at + 700
    activity = {"activity_id": "act_social", "started_at": social._born_at}
    row = {"event_id": "evt", "activity_id": "act_social",
           "event_type": "observation", "ts": now}
    for override in ({"message_id": "msg"}, {"ts": now - 601},
                     {"activity_id": "act_other"}, {"ts": now + 1}):
        assert social.opportunity(activity, [{"unit_id": "go2"}],
                                  [{**row, **override}], now=now) is None
    assert social.opportunity(activity, [], [row], now=now) is None


def test_new_activity_goal_is_one_decision_opportunity_even_when_game_is_not_ready():
    social = SocialState()
    now = social._born_at + 1
    activity = {"activity_id": "act_request", "started_at": now,
                "context": "你们两个玩石头剪刀布"}
    peers = [{"unit_id": "go2"}]
    chance = social.opportunity(activity, peers, [], idle_ready=False, now=now)
    assert chance['kind'] == 'activity_request'
    assert chance['event_ids'] == ['activity_request:act_request']
    # Neither a physical task gate nor the lack of sensor changes can swallow
    # the new goal; it stays pending until a role Graph actually evaluates it.
    assert social.opportunity(activity, peers, [], idle_ready=False, now=now + 1) == chance
    social.evaluated(chance, now=now + 1)
    assert social.opportunity(activity, peers, [], idle_ready=False, now=now + 900) is None
    next_chance = social.opportunity({**activity, 'activity_id': 'act_new', 'started_at': now + 901},
                                    peers, [], idle_ready=False, now=now + 901)
    assert next_chance['kind'] == 'activity_request'


def test_activity_goal_opportunity_does_not_replay_after_restart_or_without_peers():
    social = SocialState()
    now = social._born_at + 1
    activity = {"activity_id": "act_request", "started_at": now,
                "text": "一起玩吧"}
    assert social.opportunity(activity, [], [], now=now) is None
    assert social.opportunity(activity, [{'unit_id': 'go2'}], [], now=now)['kind'] == 'activity_request'
    restarted = SocialState()
    restarted._born_at = now + 1
    assert restarted.opportunity(activity, [{'unit_id': 'go2'}], [], now=now + 2) is None
    assert social.opportunity({**activity, 'activity_id': 'act_future', 'started_at': now + 5},
                              [{'unit_id': 'go2'}], [], now=now) is None
