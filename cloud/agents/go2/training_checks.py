import time

import pytest

from cloud.agents.common.training.check_fixtures import person_greeting_fixture
from cloud.agents.common.training.policy import training_event_intent


def test_person_match_requires_fresh_standing_and_current_revision():
    f = person_greeting_fixture()
    assert training_event_intent(f['event'], f['records'], f['catalog'], f['facts'], now=100)
    assert not training_event_intent(f['event'], f['records'], f['catalog'],
                                     {**f['facts'], 'state': {'body_state': 'lying'}}, now=100)
    assert not training_event_intent(f['event'], f['records'], f['catalog'], f['facts'], now=106)
    f['records'][0]['desired_status'] = 'paused'
    assert not training_event_intent(f['event'], f['records'], f['catalog'], f['facts'], now=100)


@pytest.mark.asyncio
async def test_runtime_graph_delivers_one_bound_action(monkeypatch, tmp_path):
    from cloud.go2 import training_events as mod
    f = person_greeting_fixture()
    now = time.time()
    monkeypatch.setenv('SSM_CHECKPOINT_DIR', str(tmp_path / 'checkpoints'))
    monkeypatch.setattr(mod, 'runtime_for', lambda _: None)
    monkeypatch.setattr(mod.go2, 'is_connected', True)
    monkeypatch.setattr(mod.go2, 'state_snapshot', lambda: {'body_state': 'standing', 'body_telemetry_fresh': True})
    monkeypatch.setattr(mod, '_build_go2_card', lambda: {'unit_id': 'go2'})
    monkeypatch.setattr(mod, 'catalog_for', lambda _: f['catalog'])
    monkeypatch.setattr(mod, 'list_records', lambda *a, **k: f['records'])
    monkeypatch.setattr(mod, 'load_runtime', lambda *a: True)
    monkeypatch.setattr(mod, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(mod, 'get_profile', lambda: {})
    calls = []
    async def execute(name, params, *, context, request_id):
        calls.append((name, params, context))
        return {'result': 'accepted'}
    monkeypatch.setattr(mod, 'execute_tool', execute)
    listener = mod.PersonTeachingListener()
    listener.configure(connected=True)
    listener._presence = True
    listener._frame_ts = now
    monkeypatch.setattr(listener, '_blocked', lambda _: False)
    from dataclasses import replace
    context = replace(listener._context, trigger_mode_epoch=listener._generation,
                      expires_at=int(now + 5))
    event = {'event_id': 'frame:1', 'kind': 'training_event', 'ts': now, 'deadline': now + 5}
    await listener.handle_event(event, context)
    assert len(calls) == 1 and calls[0][1] == {'cmd': 'Hello'}
    assert calls[0][2].trigger_ref == f['records'][0]['teaching_id']
    assert calls[0][2].actor == 'visitor'
    assert listener.last_result['result'] == 'accepted'
