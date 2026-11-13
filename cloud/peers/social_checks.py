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
