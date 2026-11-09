"""Checkpointed execution of confirmed state-machine teachings.

LangGraph owns ordered progression. Device adapters retain their existing
state, permission, reservation and real-completion boundaries.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
import inspect
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.effects import FileEffectLedger, use_effect
from cloud.agents.common.runner import invoke_blocking
from cloud.space.execution import ExecutionContext


class TeachingState(TypedDict, total=False):
    index: int
    receipts: list[dict]
    result: dict
    stopped: bool
    pending: dict


async def _call(callback, *args):
    if inspect.iscoroutinefunction(callback):
        return await callback(*args)
    value = await invoke_blocking(callback, *args)
    return await value if inspect.isawaitable(value) else value


async def run_training_workflow(prepared: dict, context: ExecutionContext, *,
                                run_id: str, snapshot, execute,
                                before_step=None, after_step=None, observe=None) -> dict:
    """Run one confirmed behavior, including a one-step behavior, identically.

    ``execute`` receives an already-resolved action, parameters, trusted step
    context and stable request ID. It must enter the original device boundary.
    Reinvocation reads the same checkpoint and never replays completed steps.
    """
    from cloud.agents.common.training import service, store
    from cloud.agents.common.training.catalog import catalog_for

    unit_id = prepared['unit_id']
    reference = prepared['training_ref']
    revision = prepared['training_revision']
    definition = prepared['record']['definition']
    key = sha256('\0'.join((context.space_id, unit_id, context.session_id,
                            run_id, reference, str(revision))).encode()).hexdigest()
    thread_id = 'teaching:' + key
    config = {'configurable': {'thread_id': thread_id},
              'recursion_limit': len(definition['steps']) + 4}
    saver, ledger = FileCheckpointSaver(), FileEffectLedger()
    execution = replace(context, training_ref=reference, training_revision=revision,
                        training_overrides=prepared.get('training_overrides') or {},
                        training_run_id=run_id)

    async def step(state: TeachingState) -> TeachingState:
        index = state.get('index', 0)
        receipts = list(state.get('receipts') or [])
        try:
            if execution.expires_at and execution.expires_at <= time.time():
                raise ValueError('execution_context_expired')
            records = await invoke_blocking(store.list_records, unit_id, space_id=execution.space_id)
            record = next((row for row in records if row['teaching_id'] == reference), None)
            if record is None or record['desired_status'] != 'enabled':
                raise ValueError('training_not_enabled')
            if record['revision'] != revision:
                raise ValueError('training_revision_changed')
            card = await invoke_blocking(service._target, unit_id, execution.space_id)
            catalog = catalog_for(card)
            if record['capability_hash'] != catalog['hash']:
                raise ValueError('capability_changed')
            current = await _call(snapshot)
            resolved = service.resolve_training_step(
                record, catalog, index, current, execution.training_overrides or {})
            trusted = replace(execution, training_step_index=index,
                              trigger_step_index=index if execution.trigger_ref else None,
                              trigger_transition_id=(resolved['transition_id']
                                                     if execution.trigger_ref else None))
            if before_step is not None:
                block = await _call(before_step, resolved, trusted)
                if block:
                    return {'index': index, 'receipts': receipts, 'stopped': True,
                            'result': block}
            request_id = 'teaching_' + key + '_' + str(index)
            if resolved.get('skip'):
                result = {'result': 'ok', 'skipped': True,
                          'reason': 'goal_observed', 'goal_state': resolved.get('goal_state')}
            else:
                identity = (unit_id, thread_id, run_id, request_id)
                reserved = await invoke_blocking(
                    ledger.reserve, *identity, resolved['action'], resolved['params'])
                previous = reserved.get('result')
                if reserved['reservation'] == 'existing':
                    # Reservations without receipts are uncertain, even if the
                    # transport may never have run. Never send again to guess.
                    result = previous or {'result': 'unknown', 'error': 'prior_dispatch_unconfirmed'}
                else:
                    with use_effect(ledger, identity):
                        result = await _call(execute, resolved['action'], resolved['params'],
                                             trusted, request_id)
                    if not isinstance(result, dict) or not result.get('result'):
                        result = {'result': 'unknown', 'error': 'invalid_device_result'}
                    await invoke_blocking(ledger.mark_result, *identity, result)
            if after_step is not None:
                await _call(after_step, resolved, trusted, result)
            receipt = {'step_index': index, 'action': resolved['action'],
                       'params': resolved['params'],
                       'transition_id': resolved['transition_id'], 'result': result,
                       'request_id': request_id}
            if result.get('result') != 'ok':
                return {'index': index, 'receipts': receipts, 'stopped': True,
                        'pending': receipt,
                        'result': {**result, 'step_index': index}}
            receipts.append(receipt)
            done = index + 1 >= len(definition['steps'])
            return {'index': index + 1, 'receipts': receipts, 'stopped': done,
                    'pending': {},
                    'result': {'result': 'ok', 'step_count': index + 1}}
        except asyncio.CancelledError:
            raise
        except (ValueError, KeyError) as exc:
            return {'index': index, 'receipts': receipts, 'stopped': True,
                    'result': {'result': 'blocked', 'error': str(exc), 'step_index': index}}

    graph = StateGraph(TeachingState)
    graph.add_node('transition', step)
    graph.add_edge(START, 'transition')
    graph.add_conditional_edges('transition',
                                lambda state: END if state.get('stopped') else 'transition',
                                {END: END, 'transition': 'transition'})
    compiled = graph.compile(checkpointer=saver)
    async with saver.async_run_lock(thread_id):
        head = await invoke_blocking(saver.get_tuple, config)
        prior = head.checkpoint.get('channel_values', {}) if head else {}
        if prior.get('stopped') and prior.get('result'):
            # A completed/paused invocation is replayed as an observation. A
            # new behavior requires a new user/event request, not a fresh ID
            # for an uncertain physical step.
            observed = None
            if prior.get('pending') and observe is not None:
                observed = await _call(observe, prior['pending'])
            return {**prior['result'], 'steps': prior.get('receipts') or [],
                    'training_ref': reference, 'training_revision': revision,
                    'training_run_id': run_id, 'replayed': True,
                    **({'last_step_result': observed} if observed is not None else {})}
        output = await compiled.ainvoke(None if prior else
                                        {'index': 0, 'receipts': [], 'stopped': False},
                                        config, durability='sync')
    return {**output['result'], 'steps': output.get('receipts') or [],
            **({'last_step_result': output['pending']['result']} if output.get('pending') else {}),
            'training_ref': reference, 'training_revision': revision,
            'training_run_id': run_id}
