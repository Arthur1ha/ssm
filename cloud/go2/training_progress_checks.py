"""Teaching highlights retain exact observation/step identity through public SSE."""

from copy import deepcopy
import json
from dataclasses import replace
import time

import pytest

from cloud.agents.common.progress_facts import public_process_facts, training_step_facts
from cloud.go2.command_request_checks import device
from cloud.go2.training_event_checks import bound_listener


def training_facts(phase, *, observation=None, result=None):
    record = {'teaching_id': 'owner-sit', 'revision': 2, 'name': '看到主人就坐下',
              'definition': {'trigger': {'event_id': 'vision.owner_recognized'}}}
    resolved = {'step_index': 0, 'transition_id': 'sit-from-standing'}
    return training_step_facts(record, resolved, run_id='appearance-one', phase=phase,
                               observation=observation, result=result)


def test_observation_dispatch_receipt_keep_causal_identity_and_original_frame_time(monkeypatch):
    from cloud.api import intents

    broker = intents._EventBroker()
    monkeypatch.setattr(intents, '_events', broker)
    channel = broker.subscribe('viewer')
    observation = {'event_id': 'owner-camera-one', 'id': 'vision.owner_recognized',
                   'frame_id': 17, 'ts': 100, 'values': {'recognized': True},
                   'context': {'private': 'must not escape'}}
    emitted = []
    for phase in ('observation', 'call', 'result'):
        facts = training_facts(phase, observation=observation,
                               result={'result': 'ok', 'body_state': 'sitting'} if phase == 'result' else None)
        emitted.append(intents.publish_agent_event('agent_progress', agent_id='go2',
            session_id='viewer', source='training', device_epoch='connection-one', **facts))
    assert [row['sequence'] for row in emitted] == [0, 1, 2]
    assert len({row['source_event_id'] for row in emitted}) == 3
    for row in emitted:
        assert row['teaching_id'] == 'owner-sit' and row['revision'] == row['version'] == 2
        assert row['transition_id'] == 'sit-from-standing'
        assert row['run_id'] == 'appearance-one' and row['step_id'] == 'appearance-one:0'
        assert row['step_index'] == 0 and row['step_number'] == 1
        assert row['observation_event_id'] == 'owner-camera-one'
        assert row['observation_id'] == 'vision.owner_recognized'
        assert row['frame_id'] == 17 and row['as_of'] == 100
        assert row['observation'] == {'event': 'vision.owner_recognized', 'as_of': 100, 'recognized': True}
        assert row['device_epoch'] == 'connection-one'
        assert 'private' not in json.dumps(row) and 'body_state' not in row.get('public_result', {})
        assert channel.get_nowait() == row
    assert emitted[-1]['public_result'] == {'result': 'ok'}
    assert training_facts('result')['source_event_id'] == emitted[-1]['source_event_id']


@pytest.mark.parametrize('change', [
    {'id': 'vision.person_presence'}, {'event_id': ''}, {'ts': float('nan')},
    {'ts': True}, {'ts': -1},
])
def test_unrelated_or_malformed_optional_observation_never_claims_a_trigger(change):
    observation = {'event_id': 'owner-camera-one', 'id': 'vision.owner_recognized',
                   'frame_id': 17, 'ts': 100, 'values': {'recognized': True}}
    facts = training_facts('call', observation={**observation, **change})
    assert facts['fact_kind'] == 'call' and facts['transition_id'] == 'sit-from-standing'
    assert not {'observation_event_id', 'observation_id', 'observation', 'as_of'} & facts.keys()
    assert public_process_facts(deepcopy(facts)) == facts


@pytest.mark.asyncio
@pytest.mark.parametrize('display_available', [True, False])
async def test_actual_teaching_workflow_publishes_exact_phases_and_survives_display_failure(
        bound_listener, monkeypatch, display_available):
    from cloud.api import intents

    adapter, robot, _, _ = bound_listener
    robot.send_command.return_value = {'result': 'ok', 'confirmation_scope': 'completion'}
    adapter._frame_id = 17
    adapter._frame_ts = time.time()
    published = []

    def publish(kind, **fields):
        assert kind == 'agent_progress'
        if not display_available:
            raise RuntimeError('display unavailable')
        published.append(public_process_facts(fields))

    monkeypatch.setattr(intents, 'publish_agent_event', publish)
    now = time.time()
    context = replace(adapter._context, trigger_mode_epoch=adapter._generation,
                      expires_at=int(now + 5))
    event = {'event_id': 'fresh-appearance', 'kind': 'training_event',
             'ts': now, 'deadline': now + 5}
    await adapter.handle_event(event, context)
    assert adapter.last_result['result'] == 'ok'
    robot.send_command.assert_awaited_once_with('Hello')
    if display_available:
        assert [item['fact_kind'] for item in published] == ['observation', 'call', 'result']
        assert [item['sequence'] for item in published] == [0, 1, 2]
        assert {item['observation_event_id'] for item in published} == {'fresh-appearance'}
        assert {item['frame_id'] for item in published} == {17}
        assert len({item['transition_id'] for item in published}) == 1
        assert len({item['run_id'] for item in published}) == 1
    await adapter.handle_event(event, context)
    assert robot.send_command.await_count == 1
    assert len(published) == (3 if display_available else 0)
