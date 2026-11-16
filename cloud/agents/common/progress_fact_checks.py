"""Public fact fixtures and counted Graph calls; no live models or hardware."""

from copy import deepcopy
import json
import time
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict, Field
import pytest

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.progress_facts import public_process_facts, resolve_public_decision, step_progress_facts
from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.space.decisions import decision_evidence


@pytest.mark.parametrize('tool,params,result,kind', [
    ('start_activity', {}, {'result': 'ok', 'activity': {
        'activity_id': 'act_fixture', 'status': 'partial', 'source_session_id': 'private',
        'devices': {'go2': {'status': 'failed', 'reason': 'robot_disconnected'},
                    'esp32_desk_led': {'status': 'joined'}}}}, 'activity'),
    ('set_led_state', {'state': 'ON'}, {'result': 'accepted', 'task_id': 'physical'}, 'action'),
    ('invoke_skill', {'unit_id': 'go2', 'skill_id': 'go2_sport', 'params': {'cmd': 'Hello'}},
        {'result': 'ok', 'tool_results': [{'step_id': 'child', 'tool': 'go2_sport',
                                         'result': {'result': 'ok'}}]}, 'action'),
    ('go2_observe', {}, {'result': 'ok', 'frame_id': 'frame', 'as_of': 123}, 'query'),
    ('apply_training', {'teaching_id': 'warm', 'revision': 2, 'unit_id': 'esp32_desk_led'},
        {'result': 'unknown', 'training_ref': 'warm', 'training_revision': 2, 'step_index': 1}, 'training'),
    ('send_message', {'target': 'go2', 'content': '原人格发言'},
        {'result': 'accepted', 'message_id': 'msg', 'delivery_status': 'received'}, 'message'),
])
def test_typed_fixtures_preserve_receipt_identity_without_private_context(tool, params, result, kind):
    step = {'step_id': 'call', 'tool_call_id': 'call', 'tool': tool, 'params': params,
            'result': {**result, 'context': {'secret': 'never-public'}},
            'effectful': kind != 'query', 'model_turn': 2, 'sequence': 20, 'ts': 123}
    projected = step_progress_facts(step)
    assert projected['step_id'] == projected['tool_call_id'] == 'call'
    assert projected['model_turn'] == 2 and projected['result_kind'] == kind
    assert projected['public_result']['result'] == result['result']
    assert 'secret' not in json.dumps(projected) and 'context' not in projected['public_result']
    assert public_process_facts(projected) == projected
    if kind == 'activity':
        members = projected['public_result']['activity']['members']
        assert members[0] == {'unit_id': 'go2', 'status': 'failed', 'reason': 'robot_disconnected'}
        assert 'private' not in json.dumps(projected)
    if kind == 'training':
        assert projected['arguments']['teaching_id'] == 'warm'


def test_search_and_game_facts_keep_only_public_nested_fields():
    projected = public_process_facts({'fact_kind': 'observation', 'result_kind': 'query',
        'observation': {'frame_id': 'frame', 'as_of': 123, 'found': None,
                        'scene_summary': '桌前有一个盒子', 'proximity': 'uncertain',
                        'context': 'private'},
        'public_result': {'candidate': {'source': 'vision', 'x': 1, 'y': 2, 'prompt': 'private'},
            'game_rules': {'rule_id': 'rps', 'status': 'public',
                           'mapping': [{'choice': 'rock', 'label': '石头', 'secret': 'private'}]},
            'secret_choice': 'scissors'}})
    assert projected['observation']['found'] is None
    assert projected['observation']['as_of'] == 123
    assert projected['public_result']['candidate'] == {'source': 'vision', 'x': 1, 'y': 2}
    assert projected['public_result']['game_rules']['mapping'] == [{'choice': 'rock', 'label': '石头'}]
    assert 'private' not in json.dumps(projected)
    assert 'secret_choice' not in projected['public_result']


def test_malformed_optional_evidence_is_bounded_and_does_not_raise():
    assert public_process_facts({'decision_basis': {'reason': '选择说明', 'evidence': {}}})['decision_basis'] == {
        'reason': '选择说明', 'evidence': []}


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    brightness: int


@pytest.mark.parametrize('case', ['valid', 'invalid', 'reason_only', 'empty_refs', 'malformed_refs', 'stale_refs'])
@pytest.mark.asyncio
async def test_display_metadata_is_not_a_business_argument_or_effect_fingerprint(tmp_path, monkeypatch, case):
    diagnostics, dispatched, progress = [], [], []
    monkeypatch.setattr('cloud.agents.common.runner.emit_agent_event', lambda event, **fields: diagnostics.append(event))
    now = time.time()
    space = {'environment': [{'source': 'light', 'status': 'current', 'as_of': now,
                              'values': {'level': 'DARK'}}]}
    ref = decision_evidence(space)[0]['ref']
    args = {'brightness': 140, 'decision_reason': '先调低亮度'}
    if case != 'reason_only':
        args['evidence_refs'] = {'valid': [ref], 'invalid': ['made-up'], 'empty_refs': [],
                                 'malformed_refs': {'ref': ref}, 'stale_refs': [ref]}[case]
    if case == 'stale_refs':
        space['environment'][0]['as_of'] = now - 3600

    class Model:
        calls = 0
        def bind_tools(self, tools, **_options): return self
        async def ainvoke(self, messages):
            self.calls += 1
            return AIMessage(content='', tool_calls=[{'name': 'adjust', 'id': 'call', 'args': args}])

    model = Model()
    ledger = FileEffectLedger(tmp_path / 'effects')
    run = GraphRun('run', 'session', '调灯', now + 60, agent_id='lamp', decision_space=deepcopy(space))
    await run_role_graph(run, model=model, tool_schemas={'adjust': Action},
        build_messages=lambda _: [], execute=lambda name, params, step_id: (
            dispatched.append(params) or {'result': 'accepted'}),
        on_progress=lambda phase, step: progress.append((phase, deepcopy(step))),
        checkpointer=FileCheckpointSaver(tmp_path / 'checkpoints'), effect_ledger=ledger)
    assert run.status == 'ok' and model.calls == 1
    assert dispatched == [{'brightness': 140}]
    assert run.steps[0]['params'] == {'brightness': 140}
    record = json.loads(next(ledger.root.glob('*.json')).read_text())
    assert record['fingerprint'] == ledger._fingerprint('adjust', {'brightness': 140})
    assert run.steps[0]['model_turn'] == 1
    assert [phase for phase, _ in progress] == ['planning', 'decision', 'dispatching', 'observed', 'summarizing']
    assert not progress[1][1]['result']
    if case == 'valid':
        assert run.steps[0]['decision_basis']['evidence'][0]['as_of'] == now
    elif case in {'reason_only', 'empty_refs'}:
        assert run.steps[0]['decision_basis'] == {'reason': '先调低亮度', 'evidence': []}
    else:
        assert 'decision_basis' not in run.steps[0]
        assert 'decision.explanation_discarded' in diagnostics


@pytest.mark.parametrize('reason,refs', [('', None), (' ' * 8, []), ('x' * 401, []),
                                      (123, None), ('说明', ['missing']), ('说明', 'missing')])
def test_invalid_explanation_never_becomes_unreferenced_evidence(reason, refs):
    with pytest.raises(ValueError):
        resolve_public_decision({}, reason, refs)


@pytest.mark.asyncio
async def test_schema_owned_required_game_evidence_reaches_original_executor(tmp_path, monkeypatch):
    monkeypatch.setattr('cloud.agents.common.runner.emit_agent_event', lambda *_a, **_k: None)

    class GameAction(Action):
        decision_reason: str
        evidence_refs: list[str] = Field(min_length=1)

    class Model:
        def bind_tools(self, *_a, **_k): return self
        async def ainvoke(self, messages):
            return AIMessage(content='', tool_calls=[{'name': 'game', 'id': 'call',
                'args': {'brightness': 140, 'decision_reason': '自填理由', 'evidence_refs': ['invented']}}])

    received = []
    def execute(name, arguments, step_id):
        received.append(arguments)
        return {'result': 'rejected', 'error': 'decision_evidence_unavailable', 'final': True}

    run = GraphRun('game', 'session', '出题', time.time() + 30, agent_id='lamp')
    await run_role_graph(run, model=Model(), tool_schemas={'game': GameAction},
        build_messages=lambda _: [], execute=execute,
        checkpointer=FileCheckpointSaver(tmp_path / 'checkpoints'),
        effect_ledger=FileEffectLedger(tmp_path / 'effects'))
    assert received == [{'brightness': 140, 'decision_reason': '自填理由', 'evidence_refs': ['invented']}]
    assert run.status == 'rejected'
    assert run.steps[0]['params'] == {'brightness': 140}
