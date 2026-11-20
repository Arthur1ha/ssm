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
from cloud.agents.common.training.policy import validate_definition
from cloud.agents.common.training.service import load_runtime, _emit
from cloud.agents.common.training.store import list_records
from cloud.agents.go2.personality.profile import get_profile
from cloud.agents.go2.workflows.free_explore import build_motivation_graph
from cloud.go2.card import _build_go2_card
from cloud.go2.actions import STABLE_BODY_STATES
from cloud.go2.connection import go2
from cloud.go2.control import control_lease
from cloud.go2.models import get_text_llm
from cloud.go2.perception.observation_schedule import TargetObservationSchedule
from cloud.go2.perception.scene_change import SceneChangeGate
from cloud.go2.space_context import prompt_space_context
from cloud.go2.state import runtime_for
from cloud.go2.tools.executor import execute_tool
from cloud.space.execution import ExecutionContext
from cloud.space.user_goals import get_goal_registry

logger = logging.getLogger(__name__)
TEACHING_JUDGMENT_TIMEOUT_S = 30
IDENTITY_OBSERVATIONS = frozenset({'vision.owner_recognized', 'vision.stranger_recognized'})


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
        self._frame_id = None
        self._frame_goal_revisions: dict[int, int] = {}
        self._task: asyncio.Task | None = None
        self._dispatching_task: asyncio.Task | None = None
        self._pending_appearance: tuple[dict, ExecutionContext] | None = None
        self._waiting_appearance: tuple[float, str] | None = None
        self._waiting_goal_revision = 0
        self._frame_goal_revision = 0
        self._context = ExecutionContext(
            'go2-teachings', 'visitor', False, origin_kind='agent_autonomous')
        self.last_result: dict | None = None

    def configure(self, *, connected: bool, owner_session_id: str = 'system') -> None:
        """Mode changes invalidate pending work without inventing appearances."""
        epoch = go2.state_snapshot().get('epoch') if connected else None
        if not connected or not self._enabled or epoch != self._device_epoch:
            self._presence = None
            self._frame_ts = 0.0
            self._frame_id = None
            self._frame_goal_revisions.clear()
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
        self._waiting_appearance = None
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
        if self._observation_id in IDENTITY_OBSERVATIONS:
            person = self._recognized_person
            if (person is None or not self._identity_matches(person)
                    or (self._observation_id == 'vision.owner_recognized'
                        and person.person_id != context.trigger_person_id)
                    or not 0 <= time.time() - person.ts <= 5):
                return False
        return (self._enabled
                and self._presence is True
                and context.trigger_mode_epoch == self._generation
                and context.session_id == self._context.session_id
                and context.origin_kind == 'agent_autonomous'
                and get_goal_registry().is_current(context.goal_revision)
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
        if self._observation_id in IDENTITY_OBSERVATIONS and detected:
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
        self._frame_id = frame['frame_id']
        self._frame_goal_revision = get_goal_registry().current()['revision']
        self._frame_goal_revisions[frame['frame_id']] = self._frame_goal_revision
        while len(self._frame_goal_revisions) > 8:
            self._frame_goal_revisions.pop(next(iter(self._frame_goal_revisions)))
        if not detected:
            self._recognized_person = None
            self._last_person_id = None
            if previous is True:
                # Revoke this source only; independent object observations
                # remain valid when a person leaves the same camera frame.
                _ObservationTeachingListener.interrupt(self)
            return
        if self._observation_id in IDENTITY_OBSERVATIONS:
            self._try_waiting_appearance()
            return  # Only the trusted identity callback can create this event.
        if previous is not True:
            self._waiting_appearance = (ts, str(frame['frame_id']))
            self._waiting_goal_revision = self._frame_goal_revision
        self._try_waiting_appearance()

    def _try_waiting_appearance(self) -> None:
        """Wait briefly for fresh body telemetry, never queue behind physical work."""
        waiting = self._waiting_appearance
        if waiting is None:
            return
        if (not 0 <= time.time() - waiting[0] <= 5
                or not get_goal_registry().is_current(self._waiting_goal_revision)
                or self._dispatching_task is not None or self._blocked(self._context)):
            self._waiting_appearance = None
            return
        if self._can_start(self._context):
            self._waiting_appearance = None
            self._submit(*waiting, goal_revision=self._waiting_goal_revision)

    def _identity_matches(self, person) -> bool:
        if self._observation_id == 'vision.stranger_recognized':
            return (person.status == 'unknown' and person.role == 'stranger'
                    and not getattr(person, 'error_code', None))
        return person.status == 'recognized' and person.role == 'owner'

    def on_identity_event(self, identity_event) -> None:
        person = identity_event.current
        if person.status == 'searching':
            # A refresh does not revoke still-fresh evidence. Its original
            # timestamp remains checked at every step; it is never extended.
            return
        previous = self._recognized_person
        self._recognized_person = None
        if not self._identity_matches(person):
            # A refresh search is not a new appearance of the same owner.
            if (self._observation_id != 'vision.stranger_recognized'
                    or person.status == 'recognized'
                    or getattr(person, 'error_code', None) in {'face_absent', 'face_changed', 'multiple_faces'}):
                self._last_person_id = None
            if previous is not None:
                self.interrupt()
            return
        if (not self._enabled or self._presence is not True
                or (self._observation_id == 'vision.owner_recognized' and not person.person_id)
                or not 0 <= time.time() - person.ts <= 5
                or not 0 <= time.time() - self._frame_ts <= 5):
            return
        self._recognized_person = person
        identity_key = person.person_id if self._observation_id == 'vision.owner_recognized' else 'unmatched-face'
        if identity_key == self._last_person_id:
            return
        self._last_person_id = identity_key
        self._waiting_appearance = (person.ts, identity_key)
        source_frame = getattr(person, 'frame_id', None)
        self._waiting_goal_revision = (self._frame_goal_revisions.get(source_frame, -1)
                                       if source_frame is not None else self._frame_goal_revision)
        self._try_waiting_appearance()

    def _submit(self, ts: float, source_id: str, *, goal_revision: int) -> None:
        self._generation += 1
        event_id = '%s:%s:%s' % (self._observation_id, ts, source_id)
        event = {'event_id': event_id, 'kind': 'training_event', 'ts': ts,
                 'deadline': time.time() + TEACHING_JUDGMENT_TIMEOUT_S}
        context = replace(self._context, trigger_mode_epoch=self._generation,
                          goal_revision=goal_revision,
                          trigger_person_id=(self._recognized_person.person_id or None
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
        values = self._observation_values()
        observed_at = self._recognized_person.ts if self._recognized_person else self._frame_ts
        candidates = training_event_candidates(
            {'id': self._observation_id, 'event_id': event['event_id'],
             'ts': observed_at,
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
                            'training_revision': record['revision'], 'training_overrides': {},
                            'observation': {'event_id': event['event_id'], 'id': self._observation_id,
                                            'frame_id': (getattr(self._recognized_person, 'frame_id', None)
                                                         if self._recognized_person else self._frame_id),
                                            'ts': observed_at, 'values': values}}
                refresh = getattr(self, 'refresh_observation', None)
                if callable(refresh):
                    prepared['refresh_observation'] = refresh
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

    def _observation_values(self) -> dict:
        return {'recognized': True} if self._recognized_person else {'detected': True}


class _TargetTeachingListener(_ObservationTeachingListener):
    """One rule-selected object description, with appearance rather than frame semantics."""

    def __init__(self, target: str, signature: tuple) -> None:
        super().__init__('vision.target_presence')
        self.target = target
        self.signature = signature
        self.references = {key for key, _revision in signature}
        self._visible_latched = False

    def configure(self, **kwargs) -> None:
        old_epoch = self._device_epoch
        super().configure(**kwargs)
        if not self._enabled or old_epoch != self._device_epoch:
            self._visible_latched = False

    def on_target_observation(self, frame: dict, detected: bool | None) -> None:
        ts = frame.get('ts')
        if (not self._enabled or type(ts) not in (int, float)
                or not math.isfinite(ts) or not self._frame_ts < ts <= time.time() + 1
                or time.time() - ts > 5 or frame.get('frame_id') is None):
            return
        previous = self._presence
        self._presence, self._frame_ts, self._frame_id = detected, ts, frame['frame_id']
        if detected is not True:
            if detected is False:
                self._visible_latched = False
            if previous is True:
                self.interrupt()
            return
        if not self._visible_latched:
            self._visible_latched = True
            self._waiting_appearance = (ts, str(frame['frame_id']) + ':' + self.target)
            self._waiting_goal_revision = frame['goal_revision']
        self._try_waiting_appearance()

    def _observation_values(self) -> dict:
        return {'target': self.target, 'detected': True}

    def allows(self, context: ExecutionContext) -> bool:
        return (self._coordinator is not None
                and self._coordinator._target_signatures.get(self.target) == self.signature
                and super().allows(context))

    def _refresh_allowed(self, context: ExecutionContext) -> bool:
        return (self._coordinator is not None
                and self._coordinator._target_signatures.get(self.target) == self.signature
                and self._enabled and self._presence is True
                and context.trigger_ref in self.references
                and context.trigger_mode_epoch == self._generation
                and context.session_id == self._context.session_id
                and context.origin_kind == 'agent_autonomous'
                and context.trigger_observation_id == self._observation_id
                and get_goal_registry().is_current(context.goal_revision)
                and context.expires_at > time.time()
                and not self._blocked(context))

    async def refresh_observation(self, context: ExecutionContext) -> dict | None:
        """Wait in the original run for camera evidence, never replay an action."""
        if not self.allows(context):
            if not self._refresh_allowed(context):
                return None
            coordinator = self._coordinator
            coordinator._target_refresh.add(self.target)
            deadline = time.monotonic() + min(10.0, max(.01, context.expires_at - time.time()))
            try:
                # This is an evidence wait in the existing Graph step. Wake
                # on observations and periodically recheck revocation too.
                while not self.allows(context):
                    if not self._refresh_allowed(context):
                        return None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    update = coordinator._target_update
                    try:
                        await asyncio.wait_for(update.wait(), timeout=min(.25, remaining))
                    except asyncio.TimeoutError:
                        pass
            finally:
                coordinator._target_refresh.discard(self.target)
        return {'frame_id': self._frame_id, 'ts': self._frame_ts,
                'values': self._observation_values()}


class PersonTeachingListener(_ObservationTeachingListener):
    """Route identity and rule-selected object facts to the same teaching Graph."""

    def __init__(self) -> None:
        super().__init__()
        self._face_listener = _ObservationTeachingListener('vision.face_presence')
        self._owner_listener = _ObservationTeachingListener('vision.owner_recognized')
        self._stranger_listener = _ObservationTeachingListener('vision.stranger_recognized')
        self._target_listeners: dict[str, _TargetTeachingListener] = {}
        self._target_signatures: dict[str, tuple] = {}
        self._target_sample_task: asyncio.Task | None = None
        self._target_generation = 0
        self._last_target_frame = None
        self._target_schedule = TargetObservationSchedule.from_environment()
        self._target_scene = SceneChangeGate()
        self._target_observing = False
        self._target_refresh: set[str] = set()
        self._target_update = asyncio.Event()
        for worker in self._workers():
            worker._coordinator = self

    def _workers(self):
        return (self, self._face_listener, self._owner_listener,
                self._stranger_listener, *self._target_listeners.values())

    def configure(self, **kwargs) -> None:
        previous_epoch = self._device_epoch
        super().configure(**kwargs)
        if not self._enabled or previous_epoch != self._device_epoch:
            self._last_target_frame = None
        self._face_listener.configure(**kwargs)
        self._owner_listener.configure(**kwargs)
        self._stranger_listener.configure(**kwargs)
        for worker in self._target_listeners.values():
            worker.configure(**kwargs)

    def interrupt(self) -> None:
        super().interrupt()
        self._face_listener.interrupt()
        self._owner_listener.interrupt()
        self._stranger_listener.interrupt()
        self._target_generation += 1
        if self._target_sample_task is not None and self._target_sample_task is not asyncio.current_task():
            self._target_sample_task.cancel()
        # Keep the lane occupied until cancellation really finishes. Some
        # providers finish despite cancellation; new requests must not overlap.
        self._target_scene = SceneChangeGate()
        self._target_schedule.request()
        self._notify_target_update()
        for worker in self._target_listeners.values():
            worker.interrupt()

    def allows(self, context: ExecutionContext) -> bool:
        if context.trigger_observation_id == 'vision.owner_recognized':
            return self._owner_listener.allows(context)
        if context.trigger_observation_id == 'vision.face_presence':
            return self._face_listener.allows(context)
        if context.trigger_observation_id == 'vision.stranger_recognized':
            return self._stranger_listener.allows(context)
        if context.trigger_observation_id == 'vision.target_presence':
            return any(context.trigger_ref in worker.references and worker.allows(context)
                       for worker in self._target_listeners.values())
        if context.trigger_observation_id not in (None, 'vision.person_presence'):
            return False
        return super().allows(context)

    def on_vision_frame(self, frame: dict) -> None:
        super().on_vision_frame(frame)
        self._face_listener.on_vision_frame(frame)
        self._owner_listener.on_vision_frame(frame)
        self._stranger_listener.on_vision_frame(frame)
        if not self._target_sampling_allowed():
            self._pause_target_observation()
            return
        if not self._target_observing:
            self._target_observing = True
            self._target_schedule.request()
            self._target_scene = SceneChangeGate()
        if (self._enabled
                and (self._target_sample_task is None or self._target_sample_task.done())):
            source = (frame.get('camera_session_id'), frame.get('source_frame_id', frame.get('frame_id')))
            if (type(source[1]) is int and (self._last_target_frame is None
                    or source[0] != self._last_target_frame[0]
                    or source[1] > self._last_target_frame[1])):
                self._last_target_frame = source
                generation = self._target_generation
                goal_revision = get_goal_registry().current()['revision']
                self._target_sample_task = asyncio.create_task(
                    self._sample_targets(frame, generation, goal_revision), name='go2-teaching-targets')
                self._target_sample_task.add_done_callback(self._target_sample_finished)

    def _target_sampling_allowed(self) -> bool:
        if not self._enabled or not go2.is_connected:
            return False
        current = go2.state_snapshot()
        if (current.get('epoch') != self._device_epoch
                or current.get('activity_state') == 'rest'
                or current.get('control_mode') == 'remote'):
            return False
        runtime = runtime_for(go2)
        if runtime is not None and runtime.active_reason():
            return False
        # An accepted taught behavior still needs evidence between its steps;
        # its own pending body action must not suspend camera observation.
        if any(worker._dispatching_task is not None for worker in self._target_listeners.values()):
            return True
        return not self._blocked(self._context)

    def _pause_target_observation(self) -> None:
        if not self._target_observing:
            return
        self._target_observing = False
        self._target_generation += 1
        self._target_scene = SceneChangeGate()
        self._target_schedule.request()
        self._invalidate_target_facts()
        task = self._target_sample_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _notify_target_update(self) -> None:
        self._target_update.set()
        self._target_update = asyncio.Event()

    def on_identity_event(self, event) -> None:
        self._owner_listener.on_identity_event(event)
        self._stranger_listener.on_identity_event(event)

    def _target_sample_finished(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.warning('Go2 teaching target observation failed: %s', type(task.exception()).__name__)
        if self._target_sample_task is task:
            self._target_schedule.cancel()
            self._target_sample_task = None

    def _read_target_signatures(self) -> dict[str, tuple]:
        catalog = catalog_for(_build_go2_card())
        targets: dict[str, list[tuple[str, int]]] = {}
        for record in list_records('go2', space_id=self._context.space_id):
            trigger = record['definition'].get('trigger') or {}
            if (record.get('desired_status') != 'enabled'
                    or record.get('capability_hash') != catalog['hash']
                    or trigger.get('event_id') != 'vision.target_presence'):
                continue
            try:
                validate_definition(record['definition'], catalog)
            except ValueError:
                continue
            targets.setdefault(trigger['where']['target'], []).append((record['teaching_id'], record['revision']))
        return {target: tuple(sorted(revisions)) for target, revisions in targets.items()}

    def _sync_target_listeners(self, signatures: dict[str, tuple]) -> None:
        if signatures != self._target_signatures:
            self._target_schedule.request()
            self._target_scene = SceneChangeGate()
        self._target_signatures = signatures
        for target, worker in list(self._target_listeners.items()):
            if signatures.get(target) != worker.signature:
                worker._enabled = False
                worker.interrupt()
                del self._target_listeners[target]
                self._notify_target_update()
        for target, signature in signatures.items():
            if target not in self._target_listeners:
                worker = _TargetTeachingListener(target, signature)
                worker._coordinator = self
                worker.configure(connected=self._enabled, owner_session_id=self._context.session_id)
                self._target_listeners[target] = worker

    def _invalidate_target_facts(self) -> None:
        # An unreadable rule store is uncertainty, not a new appearance when
        # it recovers. Keep each target's visibility latch while revoking work.
        self._target_signatures = {}
        for worker in self._target_listeners.values():
            worker._presence = None
            worker.interrupt()
        self._notify_target_update()

    async def _sample_targets(self, frame: dict, generation: int, goal_revision: int) -> None:
        from cloud.go2.perception.vision import frame_is_fresh, vision_loop
        from cloud.go2.perception.teaching_targets import observe_targets

        if not frame_is_fresh(frame):
            return
        try:
            signatures = await invoke_blocking(self._read_target_signatures)
        except (OSError, ValueError):
            if generation == self._target_generation:
                self._invalidate_target_facts()
            return
        if (generation != self._target_generation or not self._enabled
                or not get_goal_registry().is_current(goal_revision)):
            return
        if not self._target_sampling_allowed():
            self._pause_target_observation()
            return
        self._sync_target_listeners(signatures)
        if not signatures:
            return
        jpeg = vision_loop.get_frame(frame['frame_id'])
        if jpeg is None:
            return
        gate = self._target_scene
        scene = await invoke_blocking(gate.observe, jpeg, frame_id=frame['frame_id'])
        if (generation != self._target_generation or gate is not self._target_scene
                or not get_goal_registry().is_current(goal_revision)
                or not frame_is_fresh(frame)):
            return
        if not self._target_sampling_allowed():
            self._pause_target_observation()
            return
        refresh = any(target in signatures for target in self._target_refresh)
        if not self._target_schedule.due(time.monotonic(), changed=scene.changed, refresh=refresh):
            return
        targets = sorted(signatures)
        self._target_schedule.start(time.monotonic())
        failed = False
        try:
            # A late response never renews the original camera timestamp.
            async with asyncio.timeout(max(.1, 5 - (time.time() - frame['ts']))):
                observed = await observe_targets(jpeg, frame, targets)
            if (not isinstance(observed, dict) or any(target not in observed
                    or observed[target] is not None and type(observed[target]) is not bool
                    for target in targets)):
                raise ValueError('invalid_teaching_target_observation')
        except asyncio.CancelledError:
            self._target_schedule.cancel()
            raise
        except Exception:
            failed = True
            self._target_schedule.fail(time.monotonic())
            observed = {target: None for target in targets}
        if (generation != self._target_generation or not self._enabled
                or not get_goal_registry().is_current(goal_revision)
                or go2.state_snapshot().get('epoch') != self._device_epoch):
            self._target_schedule.cancel()
            return
        if not self._target_sampling_allowed():
            self._target_schedule.cancel()
            self._pause_target_observation()
            return
        try:
            current = await invoke_blocking(self._read_target_signatures)
        except (OSError, ValueError):
            self._target_schedule.cancel()
            if generation == self._target_generation:
                self._invalidate_target_facts()
            return
        if (generation != self._target_generation or not self._enabled
                or not get_goal_registry().is_current(goal_revision)
                or go2.state_snapshot().get('epoch') != self._device_epoch):
            self._target_schedule.cancel()
            return
        if not self._target_sampling_allowed():
            self._target_schedule.cancel()
            self._pause_target_observation()
            return
        self._sync_target_listeners(current)
        if not frame_is_fresh(frame) or gate is not self._target_scene:
            self._target_schedule.cancel()
            return
        if not failed:
            self._target_schedule.succeed(time.monotonic())
            gate.mark_analyzed(scene.token)
        for target in targets:
            worker = self._target_listeners.get(target)
            if worker is not None and signatures.get(target) == current.get(target):
                worker.on_target_observation({**frame, 'goal_revision': goal_revision}, observed.get(target))
        self._notify_target_update()


person_teachings = PersonTeachingListener()
