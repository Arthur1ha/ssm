from cloud.peers.social import SocialState, activity_peers


def test_activity_peers_excludes_self_unjoined_and_offline(monkeypatch):
    activity = {"activity_id": "act_one", "started_at": 1000, "status": "active",
                "devices": {"lamp": {"status": "joined"}, "dog": {"status": "joined"},
                            "guest": {"status": "offline"}}}
    monkeypatch.setattr("cloud.peers.social.get_activity_store",
                        lambda: type("Store", (), {"current": lambda self: activity})())
    current, peers = activity_peers("lamp", [
        {"unit_id": "lamp", "online": True}, {"unit_id": "dog", "online": True},
        {"unit_id": "guest", "online": False}])
    assert current is activity
    assert [peer["unit_id"] for peer in peers] == ["dog"]


def test_social_opportunities_budget_and_activity_switch():
    state = SocialState()
    state._born_at = 999
    activity = {"activity_id": "act_one", "started_at": 1000}
    peers = [{"unit_id": "dog", "online": True}]
    joined = state.opportunity(activity, peers, [], 70, now=1001)
    assert joined["kind"] == "joined"
    state.evaluated(joined, now=1001)
    assert state.opportunity(activity, peers, [], 70, now=1100) is None
    discovery = {"event_id": "evt_1", "event_type": "person_identified", "summary": "主人到了"}
    found = state.opportunity(activity, peers, [discovery], 0, now=1100)
    assert found["kind"] == "discovery"
    state.evaluated(found, now=1100)
    assert state.opportunity(activity, peers, [discovery], 0, now=1101) is None
    assert state.can_send("dog", "主人", now=1102)
    state.sent("dog", "主人", now=1102)
    assert not state.can_send("dog", "别的话题", now=1400)
    assert state.can_send("dog", "别的话题", now=1403)
    state.sent("dog", "别的话题", now=1403)
    state.sent("dog", "第三件事", now=1704)
    assert not state.can_send("dog", "第四件事", now=2005)
    assert state.opportunity(None, [], [], 70, now=1705) is None
    assert not state.can_send("dog", "新活动", now=1706)
    other = {"activity_id": "act_two", "started_at": 1707}
    assert state.opportunity(other, peers, [], 70, now=1708)["kind"] == "joined"
    assert state.can_send("dog", "主人", now=1708)


def test_new_member_gets_join_opportunity_in_older_activity_without_restart_replay():
    activity = {"activity_id": "act_old", "started_at": 100,
                "devices": {"lamp": {"status": "joined", "joined_at": 1000}}}
    peers = [{"unit_id": "dog", "online": True}]
    state = SocialState()
    state._born_at = 900
    assert state.opportunity(activity, peers, [], 70, unit_id="lamp", now=1001)["kind"] == "joined"
    state.evaluated({"kind": "joined", "event_ids": []}, now=1001)
    assert state.opportunity(activity, peers, [], 70, unit_id="lamp", now=1002) is None

    restarted = SocialState()
    restarted._born_at = 1100
    assert restarted.opportunity(activity, peers, [], 70, unit_id="lamp", now=1101) is None
