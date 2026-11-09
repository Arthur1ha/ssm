"""Isolated configuration tests; no application lifespan or device transport."""
import pytest
import time
from copy import deepcopy

from cloud.agents.common.training.store import commit, list_records
from cloud.agents.common.training.check_fixtures import training_record


def test_same_proposal_commits_only_once(monkeypatch, tmp_path):
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path))
    record = training_record()
    first = commit(record, expected_revision=0, proposal_id='p1')
    again = commit(record, expected_revision=0, proposal_id='p1')
    assert again == first
    assert len(list_records(record['unit_id'], space_id='main')) == 1
    with pytest.raises(ValueError, match='revision_conflict'):
        commit(record, expected_revision=0, proposal_id='p2')


def test_revision_preserves_history_and_invalidates_loaded(monkeypatch, tmp_path):
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path))
    record = training_record()
    first = commit(record, expected_revision=0, proposal_id='p1')
    updated = commit(first | {'name': 'revised'}, expected_revision=1, proposal_id='p2')
    assert updated['revision'] == 2
    assert updated['loaded_revision'] is None
    assert updated['history'][0]['name'] == first['name']
    assert updated['history'][0]['source_text'] == first['source_text']
    assert list_records(record['unit_id'], space_id='main') == [updated]


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    from cloud.agents.common.training import service
    from cloud.go2.card import _build_go2_card
    for key in ('SSM_TRAINING_DIR', 'SSM_TEACHINGS_DIR', 'SSM_EFFECT_LEDGER_DIR',
                'SSM_AGENT_DATA_DIR', 'SSM_CHECKPOINT_DIR'):
        monkeypatch.setenv(key, str(tmp_path / key.lower()))
    card = _build_go2_card()
    monkeypatch.setattr(service, '_targets', lambda space: {card['unit_id']: card} if space == 'main' else {})
    events = []
    monkeypatch.setattr(service, '_emit', lambda event, **context: events.append((event, context)))
    session = {'session_id': 'ses_admin', 'actor': 'admin', 'space_id': 'main',
               'expires_at': time.time() + 3600}
    session['training_source'] = service.UserText(session_id=session['session_id'], text='  看到人就打招呼。\n')
    record = training_record()
    request = {'unit_id': record['unit_id'], 'operation': 'create', 'name': 'Hello',
               'expected_revision': 0, 'definition': record['definition'],
               'capability_hash': record['capability_hash']}
    return service, session, request, card, events


def test_proposal_confirm_restore_and_revision_conflict(isolated):
    service, session, request, _, events = isolated
    proposal = service.propose(request, session)
    assert list_records(request['unit_id'], space_id='main') == []
    assert proposal['before'] is None
    assert proposal['after']['source_text'] == '  看到人就打招呼。\n'
    assert service.get_proposal(proposal['proposal_id'], session) == proposal
    saved = service.confirm(proposal['proposal_id'], 0, session)
    assert saved['loaded_revision'] is None
    assert saved['source_text'] == session['training_source'].text
    assert service.confirm(proposal['proposal_id'], 0, session) == saved
    update = request | {'operation': 'update', 'teaching_id': saved['teaching_id'], 'expected_revision': 1}
    p1, p2 = service.propose(update, session), service.propose(update, session)
    assert service.confirm(p1['proposal_id'], 1, session)['revision'] == 2
    with pytest.raises(service.TrainingError, match='revision_conflict'):
        service.confirm(p2['proposal_id'], 1, session)
    assert [item[0] for item in events].count('training.updated') == 2


def test_owner_rule_can_be_defined_before_requester_facts_are_available(isolated):
    service, session, request, _, _ = isolated
    request['definition']['conditions'] = [{'field': 'requester_is_owner', 'op': 'eq', 'value': True}]
    proposal = service.propose(request, session)
    assert proposal['confirmation_allowed'] is True
    assert service.confirm(proposal['proposal_id'], 0, session)['revision'] == 1


def test_source_and_identity_cannot_be_submitted_by_model(isolated):
    service, session, request, _, _ = isolated
    for field in ('source_text', 'training_source', 'requester_is_owner', 'space_id', 'teaching_id'):
        with pytest.raises(ValueError):
            service.propose(request | {field: 'forged'}, session)
    with pytest.raises(service.TrainingError, match='trusted_user_text_required'):
        service.propose(request, {k: v for k, v in session.items() if k != 'training_source'})
    with pytest.raises(service.TrainingError, match='trusted_user_text_required'):
        service.propose(request, session | {'training_source': {'text': 'model text'}})


def test_ownership_session_expiry_and_capability_rechecked(isolated, monkeypatch):
    service, session, request, card, _ = isolated
    proposal = service.propose(request, session)
    for bad in (session | {'actor': 'visitor'}, session | {'expires_at': 0}):
        with pytest.raises(service.TrainingError):
            service.propose(request, bad)
        with pytest.raises(service.TrainingError):
            service.confirm(proposal['proposal_id'], 0, bad)
    with pytest.raises(service.TrainingError, match='proposal_not_found'):
        service.get_proposal(proposal['proposal_id'], session | {'session_id': 'ses_other'})
    with pytest.raises(service.TrainingError, match='device_not_in_space'):
        service.snapshot(request['unit_id'], session | {'space_id': 'other'})
    for region in card['state_machine']['regions']:
        for edge in region['transitions']:
            if edge['id'] == request['definition']['transition_ids'][0]:
                edge['training_disabled'] = True
    with pytest.raises(service.TrainingError, match='capability_changed'):
        service.confirm(proposal['proposal_id'], 0, session)
    monkeypatch.setattr(service, '_targets', lambda space: {})
    with pytest.raises(service.TrainingError, match='proposal_not_found'):
        service.confirm(proposal['proposal_id'], 0, session)


def test_expired_proposal_is_gone(isolated, monkeypatch):
    service, session, request, _, _ = isolated
    proposal = service.propose(request, session)
    monkeypatch.setattr(service, '_now', lambda: proposal['expires_at'] + 1)
    with pytest.raises(service.TrainingError) as error:
        service.confirm(proposal['proposal_id'], 0, session)
    assert error.value.status_code == 410


def test_conversation_history_preserves_terminal_cards_and_session_scope(isolated, monkeypatch):
    service, session, request, _, _ = isolated
    confirmed = service.propose(request | {'name': '挥手后等一分钟', 'effect_text': '每次挥手完成后，一分钟内不重复挥手。'}, session)
    service.confirm(confirmed['proposal_id'], 0, session)
    cancelled = service.propose(request, session)
    service.cancel(cancelled['proposal_id'], session)
    expired = service.propose(request, session)
    monkeypatch.setattr(service, '_now', lambda: expired['expires_at'] + 1)
    rows = service.list_proposals(session)['proposals']
    assert [row['proposal_id'] for row in rows] == [confirmed['proposal_id'], cancelled['proposal_id'], expired['proposal_id']]
    assert rows[0]['confirmed'] and rows[1]['cancelled'] and rows[2]['expired']
    assert rows[0]['summaryText'] == '挥手后等一分钟'
    assert rows[0]['changeText'] == '每次挥手完成后，一分钟内不重复挥手。'
    assert rows[0]['source_text'] == session['training_source'].text
    assert service.get_proposal(expired['proposal_id'], session)['confirmation_allowed'] is False
    assert service.list_proposals(session | {'session_id': 'other'}) == {'proposals': []}
    with pytest.raises(service.TrainingError, match='proposal_not_found'):
        service.cancel(cancelled['proposal_id'], session | {'session_id': 'other'})


def test_pending_revision_replaces_draft_without_creating_competing_rules(isolated):
    service, session, request, _, _ = isolated
    first = service.propose(request | {'name': '挥手后等一分钟'}, session)
    second = service.propose(request | {'name': '挥手后等两分钟', 'supersedes_proposal_id': first['proposal_id'],
        'definition': {**request['definition'], 'cooldown_s': 120}}, session)
    assert second['teaching_id'] == first['teaching_id']
    assert second['beforeSummaryText'] == first['summaryText']
    assert service.get_proposal(first['proposal_id'], session)['superseded_by'] == second['proposal_id']
    with pytest.raises(service.TrainingError, match='proposal_superseded'):
        service.confirm(first['proposal_id'], 0, session)
    saved = service.confirm(second['proposal_id'], 0, session)
    assert saved['definition']['cooldown_s'] == 120
    assert len(service.snapshot(saved['unit_id'], session)['records']) == 1
    assert not service.snapshot(saved['unit_id'], session)['proposals']
    service.cancel(first['proposal_id'], session)
    with pytest.raises(service.TrainingError, match='proposal_already_confirmed'):
        service.cancel(second['proposal_id'], session)


def test_cancel_is_idempotent_and_never_changes_existing_rule(isolated):
    service, session, request, _, _ = isolated
    first = service.propose(request, session)
    saved = service.confirm(first['proposal_id'], 0, session)
    update = service.propose(request | {'operation': 'update', 'teaching_id': saved['teaching_id'],
        'expected_revision': 1, 'name': '新规则'}, session)
    assert service.cancel(update['proposal_id'], session)['cancelled']
    assert service.cancel(update['proposal_id'], session)['cancelled']
    with pytest.raises(service.TrainingError, match='proposal_cancelled'):
        service.confirm(update['proposal_id'], 1, session)
    assert list_records(saved['unit_id'], space_id='main')[0]['name'] == saved['name']


def test_load_ack_exact_versions_pause_remove_and_redaction(isolated):
    service, session, request, _, _ = isolated
    p = service.propose(request, session)
    saved = service.confirm(p['proposal_id'], 0, session)
    key = saved['teaching_id']
    revisions = {key: {'revision': 1, 'capability_hash': saved['capability_hash']}}
    internal = service.ExecutorContext(executor_id='go2', space_id='main')
    with pytest.raises(service.TrainingError):
        service.acknowledge(saved['unit_id'], revisions, session)
    service.acknowledge(saved['unit_id'], revisions, internal)
    snap = service.snapshot(saved['unit_id'], session)
    assert snap['records'][0]['load_status'] == 'loaded'
    assert snap['records'][0]['executable'] is False
    assert snap['records'][0]['execution_reasons'] == [{'code': 'device_offline'}]
    paused = service.set_enabled(key, False, 1, session)
    assert paused['revision'] == 2 and paused['desired_status'] == 'disabled'
    with pytest.raises(service.TrainingError, match='stale_loaded_revision'):
        service.acknowledge(saved['unit_id'], revisions, internal)
    visitor = service.snapshot(saved['unit_id'], session | {'actor': 'visitor'})
    assert 'source_text' not in visitor['records'][0]
    assert all('source_text' not in h for h in visitor['records'][0]['history'])
    remove = {'unit_id': saved['unit_id'], 'operation': 'remove', 'teaching_id': key,
              'expected_revision': 2, 'capability_hash': saved['capability_hash']}
    p = service.propose(remove, session)
    removed = service.confirm(p['proposal_id'], 2, session)
    assert removed['desired_status'] == 'removed' and len(removed['history']) == 2
    with pytest.raises(service.TrainingError, match='teaching_removed'):
        service.set_enabled(key, True, 3, session)


@pytest.fixture
def api(isolated, monkeypatch, tmp_path):
    from fastapi import FastAPI, Request
    from httpx import AsyncClient, ASGITransport
    from cloud.api import auth, policy, training
    service, session, request, _, events = isolated
    monkeypatch.setattr(policy, '_AUDIT_FILE', tmp_path / 'audit.jsonl')
    monkeypatch.setattr(auth, 'get_space_registry', lambda: type('Registry', (), {
        'get_space': lambda self: {'invite': {'enabled': True, 'version': 1}}})())
    monkeypatch.setenv('SSM_INTERNAL_SERVICE_TOKEN', 'isolated-training-token')
    session = {**session, 'issued_at': int(time.time()), 'csrf': 'csrf-training'}
    # Exercise real cookie decode, CSRF and role dependencies, without the
    # unrelated intents session-cleanup scheduler or application lifespan.
    async def authenticated(request: Request):
        value = request.cookies.get(auth.SESSION_COOKIE)
        if not value:
            from fastapi import HTTPException
            raise HTTPException(401, 'authentication required')
        return auth.decode_session(value)
    app = FastAPI()
    app.include_router(training.router)
    app.dependency_overrides[auth.require_session] = authenticated
    monkeypatch.setattr(auth, 'require_session', authenticated)
    monkeypatch.setattr(training, 'require_session', authenticated)
    client = AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver')
    def login(actor='admin', csrf=True):
        raw = {k: v for k, v in session.items() if k != 'training_source'}
        raw.update(actor=actor, invite_version=1)
        client.cookies.set(auth.SESSION_COOKIE, auth.encode_session(raw))
        client.cookies.set(auth.CSRF_COOKIE, session['csrf'])
        client.headers.update({'X-CSRF-Token': session['csrf'] if csrf else ''})
    login()
    return client, login, session, request, service, events


async def test_api_auth_csrf_provenance_and_zero_execution(api):
    client, login, _, request, service, events = api
    body = request | {'user_text': '  这是用户实际输入。\n'}
    login('visitor')
    assert (await client.post('/api/training/proposals', json=body)).status_code == 403
    login(csrf=False)
    assert (await client.post('/api/training/proposals', json=body)).status_code == 403
    login()
    assert (await client.post('/api/training/proposals', json=body | {'source_text': 'forged'})).status_code == 422
    proposal = (await client.post('/api/training/proposals', json=body))
    assert proposal.status_code == 201, proposal.text
    p = proposal.json()
    assert p['after']['source_text'] == body['user_text']
    assert (await client.get('/api/training', params={'unit_id': request['unit_id']})).json()['records'] == []
    assert p['transitionLabel'] and p['changeText'] and p['scopeText']
    confirm_body = {'expected_revision': 0, 'capability_hash': p['capability_hash']}
    path = f"/api/training/proposals/{p['proposal_id']}/confirm"
    saved = (await client.post(path, json=confirm_body))
    assert saved.status_code == 200, saved.text
    assert (await client.post(path, json=confirm_body)).json() == saved.json()
    assert (await client.post(path, json=confirm_body | {'expected_revision': 1})).status_code == 409
    snap = (await client.get(f"/api/devices/{request['unit_id']}/training")).json()
    assert snap['edge_annotations']
    assert snap['records'][0]['executable'] is False
    assert all('source_text' not in context for _, context in events)
    login('visitor')
    assert 'source_text' not in (await client.get('/api/training', params={'unit_id': request['unit_id']})).json()['records'][0]
    assert (await client.get(f"/api/training/proposals/{p['proposal_id']}")).status_code == 403


async def test_api_internal_ack_and_errors(api, monkeypatch):
    client, _, _, request, service, _ = api
    p = (await client.post('/api/training/proposals', json=request | {'user_text': '教你打招呼'})).json()
    confirm = {'expected_revision': 0, 'capability_hash': p['capability_hash']}
    saved = (await client.post(f"/api/training/proposals/{p['proposal_id']}/confirm", json=confirm)).json()
    path = f"/api/internal/training/{request['unit_id']}/loaded"
    body = {'executor_id': 'go2', 'revisions': {saved['teaching_id']: {'revision': 1, 'capability_hash': saved['capability_hash']}}}
    assert (await client.post(path, json=body)).status_code == 401
    headers = {'X-SSM-Service-Token': 'isolated-training-token', 'X-SSM-Session-Id': 'executor',
               'X-SSM-Actor': 'admin', 'X-SSM-Persist': 'true', 'X-SSM-Space-Id': 'main'}
    assert (await client.post(path, json=body, headers=headers)).status_code == 200
    assert (await client.post(path, json=body | {'executor_id': 'lamp'}, headers=headers)).status_code == 403
    assert (await client.put(f"/api/training/{saved['teaching_id']}/enabled", json={'enabled': False, 'expected_revision': 1})).status_code == 200
    assert (await client.post(path, json=body, headers=headers)).status_code == 409
    p = (await client.post('/api/training/proposals', json=request | {'user_text': '再教一次'})).json()
    monkeypatch.setattr(service, '_now', lambda: p['expires_at'] + 1)
    history = await client.get(f"/api/training/proposals/{p['proposal_id']}")
    assert history.status_code == 200 and history.json()['expired']
    assert (await client.get('/api/training', params={'unit_id': 'other-device'})).status_code == 404


async def test_api_history_and_cancel_follow_session_auth_and_csrf(api):
    client, login, _, request, _, _ = api
    p = (await client.post('/api/training/proposals', json=request | {'user_text': '以后这样做'})).json()
    path = f"/api/training/proposals/{p['proposal_id']}/cancel"
    login('visitor')
    assert (await client.get('/api/training/proposals')).status_code == 403
    assert (await client.post(path)).status_code == 403
    login(csrf=False)
    assert (await client.post(path)).status_code == 403
    login()
    assert (await client.post(path)).json()['cancelled']
    history = (await client.get('/api/training/proposals', params={'unit_id': request['unit_id']})).json()
    assert len(history['proposals']) == 1 and history['proposals'][0]['cancelled']
    assert (await client.post(f"/api/training/proposals/{p['proposal_id']}/confirm", json={
        'expected_revision': 0, 'capability_hash': p['capability_hash']})).status_code == 409


async def test_internal_proposal_uses_adapter_source_and_cannot_confirm(api):
    import base64
    import json
    client, _, session, request, _, _ = api
    client.cookies.clear()
    source = {'session_id': session['session_id'], 'run_id': 'run_trusted', 'text': '  实际用户原话\n'}
    headers = {'X-SSM-Service-Token': 'isolated-training-token', 'X-SSM-Session-Id': session['session_id'],
               'X-SSM-Actor': 'admin', 'X-SSM-Persist': 'true', 'X-SSM-Space-Id': 'main',
               'X-SSM-Expires-At': str(int(session['expires_at'])),
               'X-SSM-Training-Source': base64.b64encode(json.dumps(source).encode()).decode()}
    response = await client.post('/api/training/proposals', json=request, headers=headers)
    assert response.status_code == 201, response.text
    p = response.json()
    assert p['after']['source_text'] == source['text']
    assert (await client.get('/api/training', params={'unit_id': request['unit_id']}, headers=headers)).status_code == 200
    assert (await client.post('/api/training/proposals', json=request | {'user_text': 'model invention'}, headers=headers)).status_code == 422
    forged = headers | {'X-SSM-Session-Id': 'wrong_session'}
    assert (await client.post('/api/training/proposals', json=request, headers=forged)).status_code == 422
    assert (await client.post(f"/api/training/proposals/{p['proposal_id']}/confirm", json={
        'expected_revision': 0, 'capability_hash': p['capability_hash']}, headers=headers)).status_code == 401


def test_storage_corruption_and_old_root_are_not_silently_imported(monkeypatch, tmp_path):
    import json
    from cloud.agents.common.training import store
    root, legacy = tmp_path / 'new', tmp_path / 'old'
    monkeypatch.setenv('SSM_TRAINING_DIR', str(root))
    monkeypatch.setenv('SSM_TEACHINGS_DIR', str(legacy))
    legacy.mkdir()
    record = training_record()
    (legacy / 'records.json').write_text(json.dumps([record]))
    assert list_records(record['unit_id'], space_id='main') == []
    first = commit(record, expected_revision=0, proposal_id='p1')
    path = root / 'main' / f"{record['unit_id']}.json"
    raw = path.read_text()
    path.write_text('{broken')
    with pytest.raises(store.StorageError, match='unreadable'):
        list_records(record['unit_id'], space_id='main')
    with pytest.raises(store.StorageError):
        commit(first, expected_revision=1, proposal_id='p2')
    path.write_text(raw)
    data = json.loads(raw)
    data['records'][first['teaching_id']]['space_id'] = 'elsewhere'
    path.write_text(json.dumps(data))
    with pytest.raises(store.StorageError, match='invalid'):
        list_records(record['unit_id'], space_id='main')
    assert (legacy / 'records.json').read_text() == json.dumps([record])
    for bad in ('../escape', 'a/b', '', 'a.b', 'x' * 129):
        with pytest.raises(ValueError):
            list_records(bad, space_id='main')


def test_replace_failure_preserves_whole_previous_transaction(monkeypatch, tmp_path):
    from cloud.agents.common.training import store
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path))
    record = commit(training_record(), expected_revision=0, proposal_id='p1')
    path = tmp_path / 'main' / f"{record['unit_id']}.json"
    before = path.read_bytes()
    def failed(*args):
        raise OSError('simulated replace failure')
    monkeypatch.setattr(store.os, 'replace', failed)
    with pytest.raises(OSError, match='simulated'):
        commit(record | {'name': 'must not persist'}, expected_revision=1, proposal_id='p2')
    assert path.read_bytes() == before
    assert not list(path.parent.glob('*.tmp'))
    assert not list(path.parent.glob('.*.tmp'))


def test_record_and_history_caps_refuse_without_truncating(monkeypatch, tmp_path):
    from cloud.agents.common.training import store
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path))
    record = training_record()
    with store.partition_lock(record['unit_id'], space_id='main', write=True) as data:
        data['records'] = {f't_{i}': record | {'teaching_id': f't_{i}'} for i in range(500)}
    with pytest.raises(ValueError, match='device_record_limit'):
        commit(record, expected_revision=0, proposal_id='p1')
    assert len(list_records(record['unit_id'], space_id='main')) == 500
    historical = {key: value for key, value in record.items() if key != 'history'}
    full = record | {'revision': 101, 'history': [historical | {'revision': i} for i in range(1, 101)]}
    with store.partition_lock(record['unit_id'], space_id='main', write=True) as data:
        data['records'] = {full['teaching_id']: full}
    with pytest.raises(ValueError, match='history_limit'):
        commit(full, expected_revision=101, proposal_id='p2')
    assert len(list_records(record['unit_id'], space_id='main')[0]['history']) == 100


def _concurrent_commit(record, proposal, result_queue):
    try:
        result_queue.put(('saved', commit(record, expected_revision=0, proposal_id=proposal)))
    except ValueError as exc:
        result_queue.put(('error', str(exc)))


def test_cross_process_idempotency_and_revision_lock(monkeypatch, tmp_path):
    import multiprocessing
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path))
    record = training_record()
    context = multiprocessing.get_context('spawn')
    queue = context.Queue()
    processes = [context.Process(target=_concurrent_commit, args=(record, 'same', queue)) for _ in range(4)]
    for process in processes:
        process.start()
    results = [queue.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert all(result == results[0] for result in results)
    assert results[0][0] == 'saved'
    assert len(list_records(record['unit_id'], space_id='main')) == 1
    other = record | {'unit_id': 'second'}
    processes = [context.Process(target=_concurrent_commit, args=(other, f'p{i}', queue)) for i in range(2)]
    for process in processes:
        process.start()
    results = [queue.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert sorted(result[0] for result in results) == ['error', 'saved']
    assert ('error', 'revision_conflict') in results
    queue.close()


def test_partition_read_lock_orders_pause_and_loaded_ack_batch_is_atomic(isolated, monkeypatch):
    import threading
    from cloud.agents.common.training import store
    service, session, request, _, _ = isolated
    p = service.propose(request, session)
    saved = service.confirm(p['proposal_id'], 0, session)
    key = saved['teaching_id']
    revisions = {key: {'revision': 1, 'capability_hash': saved['capability_hash']}}
    ctx = service.ExecutorContext(executor_id='go2', space_id='main')
    with pytest.raises(service.TrainingError, match='stale_loaded_revision'):
        service.acknowledge(saved['unit_id'], revisions | {'missing': revisions[key]}, ctx)
    assert list_records(saved['unit_id'], space_id='main')[0]['loaded_revision'] is None
    service.acknowledge(saved['unit_id'], revisions, ctx)
    monkeypatch.setattr(service, '_BOOT_ID', 'restarted_api')
    assert service.snapshot(saved['unit_id'], session)['records'][0]['loaded_revision'] is None
    started, done = threading.Event(), threading.Event()
    errors = []
    def pause():
        started.set()
        try:
            service.set_enabled(key, False, 1, session)
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()
    with store.partition_lock(saved['unit_id'], space_id='main') as data:
        thread = threading.Thread(target=pause)
        thread.start()
        assert started.wait(timeout=1)
        assert not done.wait(timeout=0.05)
        assert data['records'][key]['desired_status'] == 'enabled'
    assert done.wait(timeout=5)
    thread.join(timeout=1)
    assert not errors
    assert list_records(saved['unit_id'], space_id='main')[0]['desired_status'] == 'disabled'


async def test_api_corrupt_storage_reports_500(api, monkeypatch, tmp_path):
    client, _, _, request, _, _ = api
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path))
    path = tmp_path / 'main' / f"{request['unit_id']}.json"
    path.parent.mkdir()
    path.write_text('{not-json')
    response = await client.get('/api/training', params={'unit_id': request['unit_id']})
    assert response.status_code == 500
    assert response.json()['detail']['code'] == 'training_storage_unreadable'
