"""Adapt fresh person observations to the existing Go2 teaching graph.

The listener owns observation deduplication and handoff generations only.
Confirmed definitions, physical eligibility and completion stay in the shared
training service and the original Go2 command boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
import logging
import math
import time

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.runner import invoke_blocking
from cloud.agents.common.training.catalog import catalog_for
from cloud.agents.common.training.policy import training_event_candidates
from cloud.agents.common.training.service import load_runtime, _emit
from cloud.agents.common.training.store import list_records
from cloud.agents.go2.personality.profile import get_profile
from cloud.agents.go2.workflows.free_explore import build_motivation_graph
from cloud.go2.card import _build_go2_card
from cloud.go2.connection import go2
from cloud.go2.control import control_lease
from cloud.go2.models import get_text_llm
from cloud.go2.space_context import prompt_space_context
from cloud.go2.state import runtime_for
from cloud.go2.tools.executor import execute_tool
from cloud.space.execution import ExecutionContext

logger = logging.getLogger(__name__)
TEACHING_JUDGMENT_TIMEOUT_S = 30


class PersonTeachingListener:
    """One mode-independent consumer; each appearance gets one graph task."""

    def __init__(self) -> None:
        self._enabled = False
        self._generation = 0
        self._device_epoch = None
        self._presence: bool | None = None
        self._frame_ts = 0.0
        self._task: asyncio.Task | None = None
        self._dispatching_task: asyncio.Task | None = None
        self._pending_appearance: tuple[dict, ExecutionContext] | None = None
        self._context = ExecutionContext(
            'go2-teachings', 'visitor', False, origin_kind='agent_autonomous')
        self.last_result: dict | None = None

    def configure(self, *, connected: bool, owner_session_id: str = 'system') -> None:
        """Mode changes invalidate pending work without inventing appearances."""
        epoch = go2.state_snapshot().get('epoch') if connected else None
        if not connected or not self._enabled or epoch != self._device_epoch:
            self._presence = None
            self._frame_ts = 0.0
        self._enabled = connected
        self._device_epoch = epoch
        self.interrupt()
        owner = owner_session_id if owner_session_id != 'system' else 'go2-teachings'
        self._context = ExecutionContext(
            owner, 'visitor', False, origin_kind='agent_autonomous')

    def interrupt(self) -> None:
        """Discard pending observations when a user takes over the device."""
        self._generation += 1
        self._pending_appearance = None
        if (self._task is not None and not self._task.done()
                and self._task is not self._dispatching_task):
            self._task.cancel()

    def _blocked(self, context: ExecutionContext) -> bool:
        if not go2.is_connected:
            return True
        snapshot = go2.state_snapshot()
        if (snapshot.get('epoch') != self._device_epoch
                or snapshot.get('control_mode') == 'remote'
                or snapshot.get('life_state') == 'play'
                or snapshot.get('pending_action')
                or snapshot.get('navigation_active')
                or snapshot.get('velocity_requested')):
            return True
        runtime = runtime_for(go2)
        if runtime is not None and runtime.active_reason():
            return True
        from cloud.go2.service import drive
        if (getattr(drive, '_user_handoff_in_progress', False) is True
                or isinstance(getattr(drive, '_pending_question_id', None), str)):
            return True
        lease = control_lease.snapshot()
        return lease is not None and lease.owner_session_id != context.session_id

    def allows(self, context: ExecutionContext) -> bool:
        """Rechecked inside the original command lock immediately before send."""
        return (self._enabled
                and self._presence is True
                and context.trigger_mode_epoch == self._generation
                and context.session_id == self._context.session_id
                and context.origin_kind == 'agent_autonomous'
                and context.expires_at > time.time()
                and 0 <= time.time() - self._frame_ts <= 5
                and not self._blocked(context))

    def _can_start(self, context: ExecutionContext) -> bool:
        snapshot = go2.state_snapshot()
        return (not self._blocked(context) and not go2._command_lock.locked()
                and snapshot.get('body_state') == 'standing'
                and snapshot.get('body_telemetry_fresh') is True)

    def on_vision_frame(self, frame: dict) -> None:
        ts = frame.get('ts')
        detected = (frame.get('persons') or {}).get('detected')
        now = time.time()
        if (not self._enabled or type(detected) is not bool
                or type(ts) not in (int, float) or not math.isfinite(ts)
                or not self._frame_ts < ts <= now + 1 or now - ts > 5
                or frame.get('frame_id') is None):
            return
        previous = self._presence
        self._presence, self._frame_ts = detected, ts
        if not detected:
            if previous is True:
                self.interrupt()
            return
        if previous is True or not self._can_start(self._context):
            return
        self._generation += 1
        event_id = 'person:%s:%s' % (ts, frame['frame_id'])
        event = {'event_id': event_id, 'kind': 'training_event', 'ts': ts,
                 'deadline': now + TEACHING_JUDGMENT_TIMEOUT_S}
        context = replace(self._context, trigger_mode_epoch=self._generation,
                          expires_at=math.ceil(event['deadline']))
        if self._task is not None and not self._task.done():
            # Only replace a semantic judgment. Physical work is never queued
            # or cancelled by a later appearance; its real receipt must finish.
            if self._task is not self._dispatching_task:
                self._pending_appearance = (event, context)
                self._task.cancel()
            return
        self._start(event, context)

    def _start(self, event: dict, context: ExecutionContext) -> None:
        self._pending_appearance = None
        self._task = asyncio.create_task(self.handle_event(event, context),
                                         name='go2-person-teaching')
        self._task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error('Go2 person teaching failed', exc_info=task.exception())
        if self._task is not task:
            return
        self._task = None
        pending, self._pending_appearance = self._pending_appearance, None
        if pending is not None and self.allows(pending[1]) and self._can_start(pending[1]):
            self._start(*pending)

    async def handle_event(self, event: dict, context: ExecutionContext) -> None:
        if not self.allows(context):
            return
        card = _build_go2_card()
        await invoke_blocking(load_runtime, card, context)
        if not self.allows(context):
            return
        snapshot = go2.state_snapshot()
        catalog = catalog_for(card)
        candidates = training_event_candidates(
            {'id': 'vision.person_presence', 'event_id': event['event_id'],
             'ts': self._frame_ts, 'values': {'detected': True}},
            list_records('go2', space_id=context.space_id), catalog,
            {'state': snapshot, 'state_fresh': snapshot.get('body_telemetry_fresh') is True},
            now=time.time())
        if not candidates:
            return
        event = {**event, 'candidates': candidates,
                 'observation': {'event': 'vision.person_presence', 'detected': True}}
        saver = FileCheckpointSaver()
        try:
            model = get_text_llm() if len(candidates) > 1 or candidates[0].get('situation') else None
        except Exception:
            model = None
        graph = build_motivation_graph(profile_loader=get_profile, checkpointer=saver, model=model)
        event_key = sha256(event['event_id'].encode()).hexdigest()
        config = {'configurable': {'thread_id': 'go2:teaching:' + event_key}}
        async with saver.async_run_lock(config['configurable']['thread_id']):
            try:
                state = await asyncio.wait_for(graph.ainvoke(
                    {'pending_event': event, 'connected': go2.is_connected,
                     'interrupted': not self.allows(context), 'pending_question': False,
                     'space_summary': prompt_space_context(), 'device_state': snapshot},
                    config, durability='sync'), timeout=max(.1, event['deadline'] - time.time()))
            except asyncio.TimeoutError:
                state = {'event_decision': 'defer', 'decision_reason': 'event_expired'}
            intent = next((candidate for candidate in candidates
                           if candidate['teaching_id'] == state.get('selected_teaching_id')), None)
            if state.get('event_decision') != 'accept' or intent is None or not self.allows(context):
                _emit('training.applied', space_id=context.space_id, unit_id='go2',
                      candidate_ids=[candidate['teaching_id'] for candidate in candidates], status='blocked',
                      reasons=[{'code': state.get('decision_reason') or 'event_interrupted'}])
                return
            context = replace(context, trigger_ref=intent['teaching_id'],
                              trigger_revision=intent['revision'],
                              trigger_transition_id=intent['transition_id'],
                              trigger_situation_confirmed=bool(intent.get('situation')))
            edge = next(edge for edge in catalog['transitions']
                        if edge['id'] == intent['transition_id'])
            self._dispatching_task = asyncio.current_task()
            try:
                result = await execute_tool(
                    'go2_sport', {'cmd': edge['action'], **intent['params']},
                    context=context, request_id='teaching_' + event_key)
                self.last_result = result
                await graph.ainvoke({'pending_event': {}, 'task_update': {
                    'stage': 'finish', 'event_id': event['event_id'], 'result': result}},
                    config, durability='sync')
            finally:
                self._dispatching_task = None
        _emit('training.applied', space_id=context.space_id, unit_id='go2',
              teaching_id=intent['teaching_id'], revision=intent['revision'],
              transition_id=intent['transition_id'], status=result.get('result', 'unknown'),
              reasons=result.get('reasons', []))


person_teachings = PersonTeachingListener()
