"""Decision references must come from live context, not invented model evidence."""

from copy import deepcopy

import pytest

from cloud.space.decisions import decision_evidence, resolve_decision


def fact_ref(space, kind):
    return next(item["ref"] for item in decision_evidence(space) if item["kind"] == kind)


@pytest.fixture
def space(monkeypatch):
    monkeypatch.setattr("cloud.space.decisions.time.time", lambda: 1000)
    return {
        "activity": {"activity_id": "act_game", "started_at": 950,
                     "context": "你们自己玩一会儿", "status": "active"},
        "peers": [{"unit_id": "go2", "name": "呆呆狗", "online": True,
                   "state_summary": {"status": "current", "as_of": 999,
                                     "life_state": "idle", "body_state": "standing"}}],
        "environment": [{"source": "lamp", "status": "current", "as_of": 999,
                         "values": {"ism": "ON", "r": 0, "g": 0, "b": 255}}],
        "constraints": [{"constraint_id": "c_one", "issued_at": 990,
                         "expires_at": 1100, "text": "别吵醒旁边的人"}],
        "recent_discoveries": [{"event_id": "evt_owl", "activity_id": "act_game",
                                "source_unit_id": "go2", "event_type": "observation",
                                "ts": 980, "summary": "地面上有一只白色猫头鹰玩偶"}],
    }


def test_reason_keeps_actual_observation_and_source_snapshot(space):
    basis = resolve_decision(space, "狗刚看见了白色玩偶，白色有东西可找。",
                             ["evt_owl", fact_ref(space, "peer_state")], require_observation=True)
    assert basis["evidence"][0] == {
        "ref": "evt_owl", "kind": "observation", "source": "go2", "as_of": 980,
        "summary": "地面上有一只白色猫头鹰玩偶"}
    assert "空闲" in basis["evidence"][1]["summary"]
    space["recent_discoveries"][0]["summary"] = "蓝色盒子"
    assert basis["evidence"][0]["summary"] == "地面上有一只白色猫头鹰玩偶"


@pytest.mark.parametrize("refs", [[], ["invented_owl"], ["evt_owl", "evt_owl"], "evt_owl"])
def test_missing_invented_or_duplicate_references_are_rejected(space, refs):
    with pytest.raises(ValueError):
        resolve_decision(space, "想玩一会儿", refs)


def test_model_cannot_supply_its_own_evidence_catalog(space):
    space["decision_evidence"] = [{"ref": "imagined", "kind": "observation",
                                    "summary": "看到了绿色球"}]
    with pytest.raises(ValueError, match="unavailable"):
        resolve_decision(space, "选择绿色", ["imagined"], require_observation=True)


@pytest.mark.parametrize("change", ["expired", "cross_activity", "message", "unconfirmed_result"])
def test_only_fresh_current_activity_observation_can_support_question(space, change):
    discovery = space["recent_discoveries"][0]
    if change == "expired":
        discovery["ts"] = 399
    elif change == "cross_activity":
        discovery["activity_id"] = "act_previous"
    elif change == "message":
        discovery["event_type"] = "message"
    else:
        discovery.update(event_type="action_result", status="accepted")
    with pytest.raises(ValueError):
        resolve_decision(space, "选择白色", ["evt_owl"], require_observation=True)


def test_stale_state_and_expired_constraints_disappear_without_relabeling(space):
    peer_ref = fact_ref(space, "peer_state")
    space["peers"][0]["state_summary"]["as_of"] = 939
    space["environment"][0]["status"] = "stale"
    space["constraints"][0]["expires_at"] = 999
    refs = {entry["ref"] for entry in decision_evidence(space)}
    assert refs == {fact_ref(space, "activity"), "evt_owl"}
    with pytest.raises(ValueError):
        resolve_decision(space, "它现在闲着", [peer_ref])


def test_live_state_alone_does_not_establish_color_object(space):
    with pytest.raises(ValueError, match="observation_required"):
        resolve_decision(space, "灯现在是蓝色", [fact_ref(space, "environment")], require_observation=True)


def test_empty_or_unbounded_reason_is_rejected(space):
    for reason in ("", "   ", "理由" * 201, None):
        with pytest.raises(ValueError, match="reason_required"):
            resolve_decision(space, reason, ["evt_owl"])


def test_evidence_summary_and_count_are_bounded(space):
    space["recent_discoveries"][0]["summary"] = "观察" * 500
    assert len(decision_evidence(space)[-1]["summary"]) == 240
    with pytest.raises(ValueError):
        resolve_decision(space, "选择白色", [f"evt_{i}" for i in range(9)])


def test_later_context_change_does_not_mutate_recorded_basis(space):
    peer_ref = fact_ref(space, "peer_state")
    basis = resolve_decision(space, "空闲时问问它是否愿意玩", [peer_ref])
    recorded = deepcopy(basis)
    space["peers"][0]["state_summary"]["life_state"] = "play"
    assert recorded == basis
    with pytest.raises(ValueError, match="unavailable"):
        resolve_decision(space, "它还是空闲", [peer_ref])
    assert "执行任务" in resolve_decision(space, "它在忙", [fact_ref(space, "peer_state")])["evidence"][0]["summary"]


def test_heartbeat_with_same_facts_does_not_invalidate_model_reference(space):
    peer_ref = fact_ref(space, "peer_state")
    space["peers"][0]["state_summary"]["as_of"] = 1000
    assert resolve_decision(space, "它现在空闲", [peer_ref])["evidence"][0]["as_of"] == 1000


@pytest.mark.parametrize("kind", ["environment", "activity", "constraint"])
def test_changed_fact_invalidates_old_reference(space, kind):
    ref = fact_ref(space, kind)
    if kind == "environment":
        space["environment"][0]["values"]["r"] = 255
    elif kind == "activity":
        space["activity"]["context"] = "我要睡觉，保持安静"
    else:
        space["constraints"][0]["text"] = "暂时不要玩游戏"
    with pytest.raises(ValueError, match="unavailable"):
        resolve_decision(space, "继续之前的选择", [ref])
