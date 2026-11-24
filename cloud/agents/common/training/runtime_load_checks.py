"""Revoked teachings must not prevent current event rules from loading."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from cloud.agents.common.training import service, store
from cloud.agents.common.training.check_fixtures import person_greeting_fixture
from cloud.agents.common.training.policy import training_event_candidates
from cloud.go2.card import _build_go2_card
from cloud.space.execution import ExecutionContext


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path / 'training'))
    card = _build_go2_card()
    fixture = person_greeting_fixture()
    current = deepcopy(fixture['records'][0])
    current['loaded_revision'] = None
    removed = deepcopy(current) | {
        'teaching_id': 'removed-teaching', 'desired_status': 'removed',
        'capability_hash': '0' * 64, 'revision': 3,
    }
    disabled = deepcopy(removed) | {
        'teaching_id': 'disabled-teaching', 'desired_status': 'disabled',
    }
    for record in (removed, disabled):
        record['history'] = [{key: deepcopy(value) for key, value in
                              (record | {'revision': revision, 'desired_status': 'enabled'}).items()
                              if key != 'history'} for revision in (1, 2)]
    with store.partition_lock('go2', space_id='main', write=True) as data:
        data['records'] = {r['teaching_id']: r for r in (removed, disabled, current)}
    monkeypatch.setattr(service, '_target', lambda *_a: card)
    monkeypatch.setattr(service, '_emit', lambda *_a, **_k: None)

    def acknowledge(unit_id, revisions, context, executor_id):
        return service.acknowledge(unit_id, revisions,
                                   service.ExecutorContext(executor_id, context.space_id))

    monkeypatch.setattr(service, '_send_loaded_ack', acknowledge)
    return SimpleNamespace(card=card, fixture=fixture, current=current,
                           context=ExecutionContext('test-load', 'visitor', False))


def test_revoked_old_capabilities_do_not_block_current_person_rule(runtime):
    f = runtime.fixture
    assert not training_event_candidates(f['event'], store.list_records('go2', space_id='main'),
                                         f['catalog'], f['facts'], now=100)
    assert service.load_runtime(runtime.card, runtime.context)
    records = store.list_records('go2', space_id='main')
    assert all(r['loaded_revision'] == r['revision'] for r in records)
    candidates = training_event_candidates(f['event'], records, f['catalog'], f['facts'], now=100)
    assert [r['teaching_id'] for r in candidates] == [runtime.current['teaching_id']]
    assert {r['teaching_id']: r['desired_status'] for r in records} == {
        'removed-teaching': 'removed', 'disabled-teaching': 'disabled',
        runtime.current['teaching_id']: 'enabled',
    }


@pytest.mark.parametrize(('key', 'version', 'executor', 'code'), [
    ('removed-teaching', {'revision': 3, 'capability_hash': '1' * 64}, 'go2', 'capability_changed'),
    ('removed-teaching', {'revision': 2, 'capability_hash': '0' * 64}, 'go2', 'stale_loaded_revision'),
    ('unknown-teaching', {'revision': 3, 'capability_hash': '0' * 64}, 'go2', 'stale_loaded_revision'),
    ('removed-teaching', {'revision': 3, 'capability_hash': '0' * 64}, 'lamp', 'executor_target_mismatch'),
])
def test_revocation_ack_still_checks_saved_identity_and_executor(runtime, key, version, executor, code):
    with pytest.raises(service.TrainingError, match=code):
        service.acknowledge('go2', {key: version}, service.ExecutorContext(executor, 'main'))
    assert all(r['loaded_revision'] is None for r in store.list_records('go2', space_id='main'))


def test_enabled_rule_still_requires_current_capabilities(runtime):
    with store.partition_lock('go2', space_id='main', write=True) as data:
        data['records']['removed-teaching']['desired_status'] = 'enabled'
    with pytest.raises(service.TrainingError, match='capability_changed'):
        service.acknowledge('go2', {'removed-teaching': {
            'revision': 3, 'capability_hash': '0' * 64}}, service.ExecutorContext('go2', 'main'))
    assert service.load_runtime(runtime.card, runtime.context)
    records = {r['teaching_id']: r for r in store.list_records('go2', space_id='main')}
    assert records['removed-teaching']['loaded_revision'] is None
    assert records[runtime.current['teaching_id']]['loaded_revision'] == 1
