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
from cloud.go2.actions import STABLE_BODY_STATES
from cloud.go2.connection import go2
from cloud.go2.control import control_lease
from cloud.go2.models import get_text_llm
from cloud.go2.space_context import prompt_space_context
from cloud.go2.state import runtime_for
from cloud.go2.tools.executor import execute_tool
from cloud.space.execution import ExecutionContext

logger = logging.getLogger(__name__)
TEACHING_JUDGMENT_TIMEOUT_S = 30


class _ObservationTeachingListener:
    """One observation source; each appearance gets one existing Graph task."""

    def __init__(self, observation_id: str = 'vision.person_presence') -> None:
        self._coordinator = None
        self._observation_id = observation_id
        self._recognized_person = None
        self._last_person_id = None
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
            self._recognized_person = None
            self._last_person_id = None
        self._enabled = connected
        self._device_epoch = epoch
        self.interrupt()
        owner = owner_session_id if owner_session_id != 'system' else 'go2-teachings'
        self._context = ExecutionContext(
            owner, 'visitor', False, origin_kind='agent_autonomous',
            trigger_observation_id=self._observation_id)

    def interrupt(self) -> None:
        """Discard pending observations when a user takes over the device."""
        self._generation += 1
        self._pending_appearance = None
        if (self._task is not None and not self._task.done()
                and self._task is not self._dispatching_task):
            self._task.cancel()

    def _blocked(self, context: ExecutionContext) -> bool:
        if self._coordinator is not None and any(
                worker is not self and worker._dispatching_task is not None
                for worker in self._coordinator._workers()):
            return True
        if not go2.is_connected:
            return True
        snapshot = go2.state_snapshot()
        if (snapshot.get('epoch') != self._device_epoch
                or snapshot.get('control_mode') == 'remote'
                or (snapshot.get('life_state') == 'play'
                    and (snapshot.get('life_task_id') != context.training_run_id
                         or snapshot.get('life_owner') != context.session_id))
                or snapshot.get('pending_action')
                or snapshot.get('navigation_active')
                or snapshot.get('velocity_requested')):
            return True
        runtime = runtime_for(go2)
        if runtime is not None and runtime.active_reason():
            return True
        from cloud.go2.service import drive, training_behavior_busy
        if training_behavior_busy(context):
            return True
        if (getattr(drive, '_user_handoff_in_progress', False) is True
                or isinstance(getattr(drive, '_pending_question_id', None), str)):
            return True
        lease = control_lease.snapshot()
        return lease is not None and lease.owner_session_id != context.session_id

    def allows(self, context: ExecutionContext) -> bool:
        """Rechecked inside the original command lock immediately before send."""
        if context.trigger_observation_id not in (None, self._observation_id):
            return False
        if self._observation_id == 'vision.owner_recognized':
            person = self._recognized_person
            if (person is None or person.status != 'recognized' or person.role != 'owner'
                    or person.person_id != context.trigger_person_id
                    or not 0 <= time.time() - person.ts <= TEACHING_JUDGMENT_TIMEOUT_S):
                return False
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
                and snapshot.get('body_state') in STABLE_BODY_STATES
                and snapshot.get('body_telemetry_fresh') is True)

    def on_vision_frame(self, frame: dict) -> None:
        ts = frame.get('ts')
        field = 'persons' if self._observation_id == 'vision.person_presence' else 'faces'
        detected = (frame.get(field) or {}).get('detected')
        if self._observation_id == 'vision.owner_recognized' and detected:
            # Identity is only confirmed for one face; multiple faces are not
            # evidence that the recognized owner is still the visible person.
            detected = frame[field].get('count') == 1
        now = time.time()
        if (not self._enabled or type(detected) is not bool
                or type(ts) not in (int, float) or not math.isfinite(ts)
                or not self._frame_ts < ts <= now + 1 or now - ts > 5
                or frame.get('frame_id') is None):
            return
        previous = self._presence
        self._presence, self._frame_ts = detected, ts
        if not detected:
            self._recognized_person = None
            self._last_person_id = None
            if previous is True:
                self.interrupt()
            return
        if self._observation_id == 'vision.owner_recognized':
            return  # Only the trusted identity callback can create this event.
        if previous is True or not self._can_start(self._context):
            return
        self._submit(ts, str(frame['frame_id']))

    def on_identity_event(self, identity_event) -> None:
        person = identity_event.current
        previous = self._recognized_person
        self._recognized_person = None
        if person.status != 'recognized' or person.role != 'owner':
            # A refresh search is not a new appearance of the same owner.
            if person.status != 'searching':
                self._last_person_id = None
            if previous is not None:
                self.interrupt()
            return
        if (not self._enabled or self._presence is not True or not person.person_id
                or not 0 <= time.time() - person.ts <= 5
                or not 0 <= time.time() - self._frame_ts <= 5):
            return
        self._recognized_person = person
        if person.person_id == self._last_person_id:
            return
        self._last_person_id = person.person_id
        if self._can_start(self._context):
            self._submit(person.ts, person.person_id)

    def _submit(self, ts: float, source_id: str) -> None:
        self._generation += 1
        event_id = '%s:%s:%s' % (self._observation_id, ts, source_id)
        event = {'event_id': event_id, 'kind': 'training_event', 'ts': ts,
                 'deadline': time.time() + TEACHING_JUDGMENT_TIMEOUT_S}
        context = replace(self._context, trigger_mode_epoch=self._generation,
                          trigger_person_id=(self._recognized_person.person_id
                                             if self._recognized_person else None),
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
                                         name='go2-teaching:' + self._observation_id)
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
        values = ({'recognized': True} if self._recognized_person else {'detected': True})
        candidates = training_event_candidates(
            {'id': self._observation_id, 'event_id': event['event_id'],
             'ts': self._recognized_person.ts if self._recognized_person else self._frame_ts,
             'values': values},
            list_records('go2', space_id=context.space_id), catalog,
            {'state': snapshot, 'state_fresh': snapshot.get('body_telemetry_fresh') is True},
            now=time.time())
        if not candidates:
            return
        observation = {'event': self._observation_id, **values}
        if self._recognized_person:
            observation.update(person_id=self._recognized_person.person_id,
                               role=self._recognized_person.role,
                               name=self._recognized_person.name,
                               score=self._recognized_person.score)
        event = {**event, 'candidates': candidates, 'observation': observation}
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
            record = next((row for row in list_records('go2', space_id=context.space_id)
                           if row['teaching_id'] == intent['teaching_id']
                           and row['revision'] == intent['revision']), None)
            if record is None:
                return
            if self._coordinator is not None:
                # A single observed appearance never queues physical work
                # behind another source's action. Keep its real receipt alive.
                for worker in self._coordinator._workers():
                    if worker is not self:
                        _ObservationTeachingListener.interrupt(worker)
            self._dispatching_task = asyncio.current_task()
            try:
                from cloud.go2.service import run_training_behavior
                prepared = {'unit_id': 'go2', 'record': record, 'catalog': catalog,
                            'steps': record['definition']['steps'],
                            'training_ref': record['teaching_id'],
                            'training_revision': record['revision'], 'training_overrides': {}}
                # The accepted event owns a bounded whole behavior; live
                # appearance, handoff and device checks remain per-step.
                context = replace(context, expires_at=math.ceil(time.time() + 180))
                result = await run_training_behavior(
                    prepared, context, run_id='teaching_' + event_key,
                    availability=self.allows)
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


class PersonTeachingListener(_ObservationTeachingListener):
    """Route existing person, face and identity facts to the same teaching Graph."""

    def __init__(self) -> None:
        super().__init__()
        self._face_listener = _ObservationTeachingListener('vision.face_presence')
        self._owner_listener = _ObservationTeachingListener('vision.owner_recognized')
        for worker in self._workers():
            worker._coordinator = self

    def _workers(self):
        return (self, self._face_listener, self._owner_listener)

    def configure(self, **kwargs) -> None:
        super().configure(**kwargs)
        self._face_listener.configure(**kwargs)
        self._owner_listener.configure(**kwargs)

    def interrupt(self) -> None:
        super().interrupt()
        self._face_listener.interrupt()
        self._owner_listener.interrupt()

    def allows(self, context: ExecutionContext) -> bool:
        if context.trigger_observation_id == 'vision.owner_recognized':
            return self._owner_listener.allows(context)
        if context.trigger_observation_id == 'vision.face_presence':
            return self._face_listener.allows(context)
        if context.trigger_observation_id not in (None, 'vision.person_presence'):
            return False
        return super().allows(context)

    def on_vision_frame(self, frame: dict) -> None:
        super().on_vision_frame(frame)
        self._face_listener.on_vision_frame(frame)
        self._owner_listener.on_vision_frame(frame)

    def on_identity_event(self, event) -> None:
        self._owner_listener.on_identity_event(event)


person_teachings = PersonTeachingListener()
