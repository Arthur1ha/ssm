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
async def test_runtime_graph_delivers_one_bound_action(monkeypatch):
    from cloud.go2.mode import free_explore as mod
    from cloud.agents.common.training import store, service
    from cloud.go2 import card
    f = person_greeting_fixture()
    now = time.time()
    monkeypatch.setattr(mod, 'runtime_for', lambda _: None)
    monkeypatch.setattr(mod.go2, 'is_connected', True)
    monkeypatch.setattr(mod.go2, 'state_snapshot', lambda: {'body_state': 'standing', 'body_telemetry_fresh': True})
    monkeypatch.setattr(card, '_build_go2_card', lambda: {'unit_id': 'go2'})
    monkeypatch.setattr('cloud.agents.common.training.catalog.catalog_for', lambda _: f['catalog'])
    monkeypatch.setattr(store, 'list_records', lambda *a, **k: f['records'])
    monkeypatch.setattr(service, 'load_runtime', lambda *a: True)
    calls = []
    async def execute(name, params, *, context):
        calls.append((name, params, context))
        return {'result': 'accepted'}
    monkeypatch.setattr(mod, 'execute_tool', execute)
    drive = mod.Drive()
    from cloud.space.execution import ExecutionContext
    drive._context = ExecutionContext('auto', 'visitor', False, origin_kind='agent_autonomous')
    event = {'event_id': 'frame:1', 'kind': 'training_event', 'ts': now, 'deadline': now + 5}
    await drive._handle_event(event, None, None, None)
    assert len(calls) == 1 and calls[0][1] == {'cmd': 'Hello'}
    assert calls[0][2].trigger_ref == f['records'][0]['teaching_id']
    assert calls[0][2].actor == 'visitor'
    assert drive._last_tool_result['result'] == 'accepted'
