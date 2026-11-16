"""Presentation metadata cannot reject a valid game or become a game result."""

import pytest

from cloud.agents.go2.workflows.social import SocialDecision
from cloud.agents.go2.workflows.free_explore import EventDecision
from cloud.peers.games.rps_decisions import Agreement as RpsAgreement, ThrowDecision
from cloud.peers.games.delivery_decisions import Agreement as DeliveryAgreement, NextDecision
from cloud.space.activity import ActivityStore


@pytest.mark.parametrize('emotion,expected', [
    ('happy', 'happy'), ('sad', 'sad'), (None, 'neutral'),
    (['angry'], 'neutral'), ({'name': 'happy'}, 'neutral'), ('excited', 'neutral'), (1, 'neutral'),
])
@pytest.mark.parametrize('schema,values', [
    (RpsAgreement, {'accepted': True, 'speech': '同意这局规则。'}),
    (DeliveryAgreement, {'accepted': False, 'speech': '现在先等一等。'}),
    (ThrowDecision, {'choice': 'rock', 'speech': '我选好了。'}),
    (NextDecision, {'action': 'stop', 'speech': '我先停下。'}),
    (SocialDecision, {'decision': 'skip'}),
    (EventDecision, {'choice': 'accept', 'reason': '愿意参加。'}),
])
def test_invalid_emotion_does_not_change_business_decision(schema, values, emotion, expected):
    parsed = schema.model_validate({**values, 'emotion': emotion})
    assert parsed.emotion == expected
    for key, value in values.items():
        assert getattr(parsed, key) == value


def test_emotion_does_not_relax_game_business_validation():
    with pytest.raises(ValueError):
        ThrowDecision(choice='invalid', emotion='happy')
    with pytest.raises(ValueError):
        NextDecision(action='push', speech='试试。', distance_m=100, emotion='happy')
    with pytest.raises(ValueError):
        RpsAgreement(accepted='yes', speech='同意。', emotion=None)


def test_activity_emotion_is_display_only_and_does_not_create_game_result(tmp_path):
    store = ActivityStore(tmp_path / 'activity.json')
    revision = store.note_user_intervention('request')
    activity = store.begin('一起玩。', 'request', revision)
    event = store.record_event(activity['activity_id'], 'message', unit_id='go2',
                               text='我选好了。', emotion='happy', stage='throw_announcement',
                               game_id='rps_siege', game_round={'winner': 'go2'})
    assert event['emotion'] == 'happy' and 'game_round' not in event
    invalid = store.record_event(activity['activity_id'], 'message', unit_id='go2',
                                 text='稍等。', emotion={'name': 'angry'})
    assert invalid['emotion'] == 'neutral' and invalid['text'] == '稍等。'
    action = store.record_event(activity['activity_id'], 'action_result', unit_id='go2',
                                text='未确认。', status='unknown', emotion='happy')
    assert action['status'] == 'unknown' and 'emotion' not in action
