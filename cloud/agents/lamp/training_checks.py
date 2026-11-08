from cloud.agents.common.training.check_fixtures import lamp_sound_fixture
from cloud.agents.common.training.policy import training_event_intent


def test_sound_event_keeps_bound_count_and_requires_off():
    f = lamp_sound_fixture()
    result = training_event_intent(f['event'], f['records'], f['catalog'], f['facts'], now=100)
    assert result['params'] == {'count': 2}
    assert not training_event_intent(f['event'], f['records'], f['catalog'],
                                     {**f['facts'], 'state': {'ism': 'ON'}}, now=100)
    assert not training_event_intent(f['event'], f['records'], f['catalog'], f['facts'], now=106)
