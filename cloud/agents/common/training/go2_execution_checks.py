"""Isolated Go2 teaching enforcement using existing receipt and request ledgers."""
import time
import pytest
from cloud.go2.command_request_checks import device
from cloud.space.execution import ExecutionContext
from cloud.agents.common.training import store, service
from cloud.agents.common.training.check_fixtures import hello_cooldown

USER = ExecutionContext('training-user', 'admin', True, expires_at=int(time.time()) + 3600)

@pytest.fixture
def trained(device, monkeypatch, tmp_path):
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path / 'training'))
    monkeypatch.setattr(service, '_send_loaded_ack', lambda *args: None, raising=False)
    record = hello_cooldown(60)
    with store.partition_lock('go2', space_id='main', write=True) as data:
        data['records'][record['teaching_id']] = record
    return device[0], record

@pytest.mark.asyncio
async def test_real_completion_starts_cooldown_and_duplicate_does_not_dispatch(trained):
    from cloud.go2.tools.executor import execute_command_request
    robot, _ = trained
    robot.send_command.return_value = {'result': 'ok', 'confirmation_scope': 'completion'}
    first = await execute_command_request('Hello', {}, context=USER, request_id='first')
    retry = await execute_command_request('Hello', {}, context=USER, request_id='first')
    second = await execute_command_request('Hello', {}, context=USER, request_id='second')
    assert first['result'] == 'ok' and retry['replayed']
    assert second['result'] == 'blocked'
    assert second['reasons'][0]['code'] == 'cooldown'
    assert robot.send_command.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('origin', ['user_delegated', 'agent_autonomous'])
async def test_accepted_and_timeout_remain_unknown_until_real_completion(trained, origin):
    from dataclasses import replace
    from cloud.go2.tools.executor import execute_command_request
    from cloud.go2.tasks import observe_command_results
    robot, _ = trained
    context = replace(USER, origin_kind=origin, persist=origin != 'agent_autonomous')
    first = await execute_command_request('Hello', {}, context=context, request_id='pending')
    assert first['result'] == 'accepted'
    second = await execute_command_request('Hello', {}, context=context, request_id='pending-2')
    assert second['reasons'][0]['code'] == 'completion_history_unknown'
    robot.runtime.commit('timeout', last_action={'request_id': 'physical-1', 'result': 'timeout'})
    observe_command_results(robot)
    third = await execute_command_request('Hello', {}, context=context, request_id='pending-3')
    assert third['reasons'][0]['code'] == 'completion_history_unknown'
    robot.runtime.commit('complete', last_action={'request_id': 'physical-1', 'result': 'ok', 'confirmation': 'joint_posture'})
    observe_command_results(robot)
    # Reconstruct from disk; clear all live completion indexes.
    robot._command_result_records.clear()
    fourth = await execute_command_request('Hello', {}, context=context, request_id='pending-4')
    assert fourth['reasons'][0]['code'] == 'cooldown'
    assert robot.send_command.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['device_chat', 'steward', 'peer'])
async def test_graph_identity_and_completion_use_explicit_trusted_metadata(trained, source):
    from cloud.agents.common.effects import FileEffectLedger, use_effect
    from cloud.go2.tools.executor import execute_tool
    from cloud.go2.tasks import observe_command_results
    robot, _ = trained
    ledger = FileEffectLedger()
    identity = (source, 'ordinary-thread-not-json', 'run-1', 'step-1')
    ledger.reserve(*identity, 'go2_sport', {'cmd': 'Hello'})
    with use_effect(ledger, identity):
        result = await execute_tool('go2_sport', {'cmd': 'Hello'}, context=USER)
    assert result['result'] == 'accepted'
    robot.runtime.commit('completed', last_action={'request_id': 'physical-1', 'result': 'ok', 'confirmation': 'joint_posture'})
    observe_command_results(robot)
    assert ledger.read(*identity[:3], identity[3] + ':completion')['result']['result'] == 'ok'
    identity2 = (*identity[:3], 'step-2')
    ledger.reserve(*identity2, 'go2_sport', {'cmd': 'Hello'})
    with use_effect(ledger, identity2):
        blocked = await execute_tool('go2_sport', {'cmd': 'Hello'}, context=USER)
    assert blocked['reasons'][0]['code'] == 'cooldown'
    assert robot.send_command.await_count == 1


@pytest.mark.asyncio
async def test_owner_only_wake_requires_requester_not_admin(trained):
    from dataclasses import replace
    from cloud.agents.common.training.catalog import catalog_for
    from cloud.go2.card import _build_go2_card
    from cloud.go2.service import perform_life_control
    robot, record = trained
    edge = next(edge for edge in catalog_for(_build_go2_card())['transitions'] if edge['trigger'] == 'wake')
    record['definition'].update(transition_ids=[edge['id']], cooldown_s=None,
                              conditions=[{'field': 'requester_is_owner', 'op': 'eq', 'value': True}])
    with store.partition_lock('go2', space_id='main', write=True) as data:
        data['records'][record['teaching_id']] = record
    robot.runtime.commit('rest', activity_state='rest', life_state=None)
    for requester in (None, {'verified': True, 'is_owner': False, 'expires_at': time.time()+60},
                      {'verified': True, 'is_owner': True, 'expires_at': time.time()-1}):
        blocked = await perform_life_control('wake', replace(USER, requester=requester))
        assert blocked['result'] == 'blocked'
        assert robot.runtime.data['activity_state'] == 'rest'
    allowed = await perform_life_control('wake', replace(USER, requester={
        'verified': True, 'is_owner': True, 'expires_at': time.time()+60}))
    assert allowed['result'] == 'ok'
    assert robot.runtime.data['activity_state'] == 'active'
    robot.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_request_is_reserved_before_transport_and_pause_is_rechecked(trained, monkeypatch):
    import asyncio
    from cloud.go2.tools.executor import execute_command_request
    import cloud.go2.service as device_service
    robot, record = trained
    entered, finish = asyncio.Event(), asyncio.Event()
    async def send(command):
        entered.set()
        await finish.wait()
        return {'result': 'ok', 'confirmation_scope': 'completion'}
    robot.send_command.side_effect = send
    first = asyncio.create_task(execute_command_request('Hello', {}, context=USER, request_id='racing-1'))
    await entered.wait()
    second = await execute_command_request('Hello', {}, context=USER, request_id='racing-2')
    assert second['reasons'][0]['code'] == 'completion_history_unknown'
    finish.set()
    await first
    # Pause during an await before final reservation; old guard must not apply.
    async def pause(_context):
        record['desired_status'] = 'disabled'
        with store.partition_lock('go2', space_id='main', write=True) as data:
            data['records'][record['teaching_id']] = record
    # A first-phase guard would correctly deny cooldown; use a fresh first-enable record.
    record['teaching_id'] = 'replacement'
    with store.partition_lock('go2', space_id='main', write=True) as data:
        data['records'] = {record['teaching_id']: record}
    monkeypatch.setattr(device_service, 'prepare_user_interaction', pause)
    result = await execute_command_request('Hello', {}, context=USER, request_id='after-pause')
    assert result['result'] == 'ok' and result['training_refs'] == []


@pytest.mark.asyncio
async def test_scope_revision_and_restart_keep_first_enable_history(trained):
    from copy import deepcopy
    from dataclasses import replace
    from cloud.go2.tools.executor import execute_command_request
    robot, record = trained
    robot.send_command.return_value = {'result': 'ok', 'confirmation_scope': 'completion'}
    assert (await execute_command_request('Hello', {}, context=USER, request_id='baseline'))['result'] == 'ok'
    updated = deepcopy(record)
    updated['definition']['cooldown_s'] = 30
    updated = store.commit(updated, expected_revision=1, proposal_id='revision-2')
    blocked = await execute_command_request('Hello', {}, context=USER, request_id='restarted')
    assert blocked['reasons'][0]['code'] == 'cooldown'
    assert blocked['training_refs'][0]['revision'] == 2
    autonomous = replace(USER, origin_kind='agent_autonomous', persist=False)
    assert (await execute_command_request('Hello', {}, context=autonomous, request_id='other-origin'))['result'] == 'ok'
    assert robot.send_command.await_count == 2


@pytest.mark.asyncio
async def test_unknown_state_and_stop_safety(trained, monkeypatch):
    from cloud.go2.tools.executor import execute_command_request
    robot, _ = trained
    robot._sport_state_observed_at = time.monotonic() - 100
    blocked = await execute_command_request('Hello', {}, context=USER, request_id='stale')
    assert blocked['result'] == 'blocked'
    robot.send_command.assert_not_awaited()
    monkeypatch.setattr(service, 'load_runtime', lambda *args: pytest.fail('stop must not load training'))
    robot.send_command.return_value = {'result': 'ok', 'confirmation_scope': 'completion'}
    assert (await execute_command_request('StopMove', {}, context=USER, request_id='stop'))['result'] == 'ok'
    robot.send_command.assert_awaited_once()


def test_loading_ack_does_not_write_config_and_snapshot_does_not_equate_loaded_with_ready(trained, monkeypatch):
    from cloud.go2.card import _build_go2_card
    robot, record = trained
    sent = []
    monkeypatch.setattr(service, '_send_loaded_ack', lambda *args: sent.append(args))
    with store.partition_lock('go2', space_id='main', write=True) as data:
        data['records'][record['teaching_id']]['loaded_revision'] = None
    service.load_runtime(_build_go2_card(), USER)
    assert len(sent) == 1
    with store.partition_lock('go2', space_id='main') as data:
        assert data['records'][record['teaching_id']]['loaded_revision'] is None
    monkeypatch.setattr(service, '_target', lambda *args: _build_go2_card())
    service.acknowledge('go2', {record['teaching_id']: {'revision': 1, 'capability_hash': record['capability_hash']}},
                        service.ExecutorContext('go2', 'main'))
    # Snapshot reads the real module-owned runtime, which remains disconnected in this test.
    snap = service.snapshot('go2', {'session_id': USER.session_id, 'actor': 'admin',
                                   'space_id': 'main', 'expires_at': USER.expires_at})
    assert snap['records'][0]['load_status'] == 'loaded'
    assert snap['records'][0]['executable'] is False
    assert snap['records'][0]['execution_reasons'] == [{'code': 'device_offline'}]


@pytest.mark.asyncio
async def test_runner_propagates_existing_effect_identity_to_device(trained, tmp_path):
    from langchain_core.messages import AIMessage, HumanMessage
    from cloud.agents.common.runner import GraphRun, run_role_graph
    from cloud.agents.common.checkpoint import FileCheckpointSaver
    from cloud.agents.common.effects import FileEffectLedger, current_effect
    from cloud.go2.tools.executor import execute_tool
    robot, _ = trained
    seen = []
    class Model:
        def bind_tools(self, tools, **kwargs):
            return self
        async def ainvoke(self, messages):
            return AIMessage(content='', tool_calls=[{'name': 'go2_sport', 'args': {'cmd': 'Hello'}, 'id': 'graph-hello'}])
    async def execute(name, params, step_id):
        seen.append(current_effect())
        return await execute_tool(name, params, context=USER)
    ledger = FileEffectLedger()
    run = GraphRun('run-hello', USER.session_id, 'greet', time.time()+60, agent_id='go2')
    await run_role_graph(run, model=Model(), execute=execute,
        tool_schemas={'go2_sport': {'type': 'function', 'function': {'name': 'go2_sport', 'description': 'greet',
            'parameters': {'type': 'object', 'properties': {'cmd': {'type': 'string'}}, 'required': ['cmd']}}}},
        build_messages=lambda _: [HumanMessage(content='greet')],
        checkpointer=FileCheckpointSaver(tmp_path / 'checkpoint'), effect_ledger=ledger)
    assert len(seen) == 1 and seen[0][0] is ledger
    assert seen[0][1][0] == 'go2' and seen[0][1][2:] == ('run-hello', 'graph-hello')
    assert current_effect() is None
    robot.runtime.commit('complete', last_action={'request_id': 'physical-1', 'result': 'ok', 'confirmation': 'joint_posture'})
    assert ledger.read(*seen[0][1][:3], 'graph-hello:completion')['result']['result'] == 'ok'


@pytest.mark.asyncio
async def test_event_teaching_does_not_restrict_manual_and_exact_trigger_is_required(trained):
    from dataclasses import replace
    from cloud.go2.tools.executor import execute_command_request
    robot, record = trained
    record['definition']['trigger'] = {'event_id': 'vision.person_presence', 'where': {'detected': True}, 'params': {}}
    record['definition']['origins'] = ['agent_autonomous']
    with store.partition_lock('go2', space_id='main', write=True) as data:
        data['records'][record['teaching_id']] = record
    robot.send_command.return_value = {'result': 'ok', 'confirmation_scope': 'completion'}
    assert (await execute_command_request('Hello', {}, context=USER, request_id='manual'))['result'] == 'ok'
    event = replace(USER, origin_kind='agent_autonomous', persist=False, trigger_ref=record['teaching_id'],
                    trigger_revision=record['revision'], trigger_transition_id=record['definition']['transition_ids'][0])
    assert (await execute_command_request('Hello', {}, context=event, request_id='event'))['result'] == 'ok'
    blocked = await execute_command_request('Hello', {}, context=event, request_id='event-again')
    assert blocked['reasons'][0]['code'] == 'cooldown'
    assert (await execute_command_request('Hello', {}, context=USER, request_id='manual-again'))['result'] == 'ok'
    forged = replace(event, trigger_ref='not-confirmed')
    assert (await execute_command_request('Hello', {}, context=forged, request_id='forged'))['result'] == 'blocked'
    assert robot.send_command.await_count == 3


@pytest.mark.asyncio
async def test_snapshot_reports_real_cooldown_when_loaded(trained, monkeypatch):
    from cloud.go2.card import _build_go2_card
    from cloud.go2.tools.executor import execute_command_request
    import cloud.go2.connection as connection
    robot, record = trained
    monkeypatch.setattr(connection, 'go2', robot)
    monkeypatch.setattr(service, '_target', lambda *args: _build_go2_card())
    service.acknowledge('go2', {record['teaching_id']: {'revision': 1, 'capability_hash': record['capability_hash']}},
                        service.ExecutorContext('go2', 'main'))
    viewer = {'session_id': USER.session_id, 'actor': 'admin', 'space_id': 'main', 'expires_at': USER.expires_at}
    assert service.snapshot('go2', viewer)['records'][0]['executable'] is True
    robot.send_command.return_value = {'result': 'ok', 'confirmation_scope': 'completion'}
    await execute_command_request('Hello', {}, context=USER, request_id='snapshot-complete')
    record = service.snapshot('go2', viewer)['records'][0]
    assert record['load_status'] == 'loaded' and record['executable'] is False
    assert any(reason['code'] == 'cooldown' for reason in record['execution_reasons'])


@pytest.mark.asyncio
async def test_wake_retry_uses_original_request_before_current_state_or_training(trained):
    from cloud.go2.service import perform_life_control
    robot, _ = trained
    robot.runtime.commit('rest', activity_state='rest', life_state=None)
    first = await perform_life_control('wake', USER, request_id='wake-retry')
    assert first['result'] == 'ok' and robot.runtime.data['activity_state'] == 'active'
    revision = robot.runtime.data['revision']
    retry = await perform_life_control('wake', USER, request_id='wake-retry')
    assert retry['result'] == 'ok' and retry['replayed'] is True
    assert robot.runtime.data['revision'] == revision
    with pytest.raises(ValueError, match='effect_call_id_conflict'):
        await perform_life_control('request_rest', USER, request_id='wake-retry')


def test_first_version_does_not_advertise_remote_velocity_or_exploration_training():
    from cloud.agents.common.training.catalog import catalog_for
    from cloud.go2.card import _build_go2_card
    edges = catalog_for(_build_go2_card())['transitions']
    excluded = {'Move', 'EnterMoveControl', 'BalanceStand', 'start_explore', 'stop_explore', 'StopMove'}
    assert all(not edge['trainable'] for edge in edges if edge['trigger'] in excluded)
    assert all(any(edge['trainable'] and edge['trigger'] == command for edge in edges)
               for command in ('Hello', 'wake', 'request_rest'))


@pytest.mark.asyncio
async def test_transport_error_without_terminal_device_observation_is_unknown(trained):
    from cloud.go2.tools.executor import execute_command_request
    robot, _ = trained
    robot.send_command.return_value = {'result': 'error', 'error': 'transport_disconnected', 'confirmation_scope': 'request'}
    await execute_command_request('Hello', {}, context=USER, request_id='uncertain-error')
    result = await execute_command_request('Hello', {}, context=USER, request_id='after-error')
    assert result['result'] == 'blocked'
    assert result['reasons'][0]['code'] == 'completion_history_unknown'
    assert robot.send_command.await_count == 1
