"""Go2 behavior and mode side effects, shared by HTTP and model tools.

The connection runtime owns mode and behavior facts. This module wires vision,
navigation and user handoff; it does not infer physical completion.
"""
import asyncio
import logging
import time
from dataclasses import replace

from cloud.go2.state import runtime_for
from cloud.go2.connection import go2
from cloud.go2.mode.free_explore import drive
from cloud.go2.perception.vision import vision_loop
from cloud.go2.perception.identity import IdentityEvent, identity_skill
from cloud.agents.go2.memory.episode import EventType, episode_memory
from cloud.go2.control import control_lease
from cloud.space.execution import ExecutionContext, SYSTEM_CONTEXT, current_context, use_context
from cloud.go2.card import _publish_go2_card
from cloud.go2.training_events import person_teachings

_active_rule_cb = None
_active_drive_cb = None
_active_identity_cb = None
_activity_mode_owner: str | None = None
_training_behaviors: dict[str, dict] = {}


def interrupt_training_behavior(context: ExecutionContext) -> None:
    """A new authorized user command ends pending taught transitions."""
    if context.training_run_id:
        return
    for entry in tuple(_training_behaviors.values()):
        if context.actor != 'admin' and entry['session_id'] != context.session_id:
            continue
        entry['interrupted'] = True
        task = entry['task']
        if task is not asyncio.current_task() and not task.done():
            task.cancel()


def training_behavior_busy(context: ExecutionContext) -> bool:
    return any(key != context.training_run_id and not entry['task'].done()
               for key, entry in _training_behaviors.items())


def note_training_lease(context: ExecutionContext, lease) -> None:
    entry = _training_behaviors.get(context.training_run_id)
    if (entry is not None and not entry['interrupted']
            and entry['session_id'] == context.session_id):
        entry['lease_generation'] = lease.generation


async def run_training_behavior(prepared: dict, context: ExecutionContext, *,
                                run_id: str, availability=None) -> dict:
    """Execute a confirmed Go2 behavior through the original state boundaries."""
    from cloud.agents.common.training.workflow import run_training_workflow
    from cloud.go2.actions import SPORT_COMMANDS
    from cloud.go2.tools.executor import execute_tool
    from cloud.go2.tools.registry import CARD_SKILLS
    from cloud.go2.control import session_tasks

    if prepared['unit_id'] != 'go2':
        return {'result': 'rejected', 'error': 'training_target_mismatch'}
    existing = _training_behaviors.get(run_id)
    if existing is not None and existing['task'] is not asyncio.current_task() and not existing['task'].done():
        return {'result': 'accepted', 'error': 'training_already_running', 'training_run_id': run_id}
    if training_behavior_busy(replace(context, training_run_id=run_id)):
        return {'result': 'blocked', 'error': 'training_behavior_busy'}
    task = asyncio.current_task()
    entry = {'session_id': context.session_id, 'task': task, 'interrupted': False,
             'epoch': go2.state_snapshot().get('epoch'), 'lease_generation': None}
    _training_behaviors[run_id] = entry
    session_tasks.track(context.session_id, task)
    own_life = None
    last_result = {'result': 'unknown'}

    async def before_step(resolved, trusted):
        nonlocal own_life
        current = go2.state_snapshot()
        if (entry['interrupted'] or not go2.is_connected
                or current.get('epoch') != entry['epoch']):
            return {'result': 'cancelled', 'error': 'training_interrupted'}
        if availability is not None and not availability(trusted):
            return {'result': 'blocked', 'error': 'autonomy_unavailable'}
        lease = control_lease.snapshot()
        if ((lease is not None and lease.owner_session_id != trusted.session_id)
                or entry['lease_generation'] is not None and
                (lease is None or lease.generation != entry['lease_generation'])):
            return {'result': 'cancelled', 'error': 'control_lease_transferred'}
        if (current.get('control_mode') == 'remote' or current.get('pending_action')
                or current.get('navigation_active') or current.get('velocity_requested')):
            return {'result': 'blocked', 'error': 'device_busy'}
        waking = resolved['action'] == 'LIFE' and resolved['params'].get('action') == 'wake'
        if current.get('activity_state') != 'active' and not waking:
            return {'result': 'blocked', 'error': 'device_resting'}
        if resolved['action'] == 'LIFE' and resolved['params'].get('action') == 'request_rest' and own_life:
            go2.runtime.finish_life(own_life)
            own_life = None
        return None

    async def execute(action, params, trusted, request_id):
        if action in SPORT_COMMANDS:
            name, arguments = 'go2_sport', {'cmd': action, **params}
        else:
            name = next((spec['tool_name'] for spec in CARD_SKILLS.values()
                         if spec.get('tool_name') and (spec.get('invoke') or {}).get('action') == action), '')
            arguments = params
        if not name:
            return {'result': 'rejected', 'error': 'training_action_unavailable'}
        return await execute_tool(name, arguments, context=trusted, request_id=request_id)

    async def after_step(resolved, trusted, result):
        nonlocal own_life
        runtime = runtime_for(go2)
        if (result.get('result') == 'ok' and runtime is not None
                and resolved['step_index'] + 1 < len(prepared['steps'])
                and not runtime.active_reason() and runtime.life_task_available):
            own_life = runtime.begin_life('play', trusted.session_id, run_id)

    def observe(receipt):
        from cloud.go2.tasks import read_command_result
        return read_command_result(context, receipt['request_id'], robot=go2)

    async def heartbeat():
        while True:
            await asyncio.sleep(min(5.0, control_lease.ttl_s / 3))
            lease = control_lease.snapshot()
            if (not entry['interrupted'] and lease is not None
                    and lease.owner_session_id == context.session_id
                    and lease.generation == entry['lease_generation']):
                control_lease.heartbeat_session(context.session_id,
                                                generation=entry['lease_generation'])

    renewal = asyncio.create_task(heartbeat(), name='go2-teaching-lease')
    try:
        last_result = await run_training_workflow(
            prepared, context, run_id=run_id, snapshot=go2.state_snapshot,
            execute=execute, before_step=before_step, after_step=after_step, observe=observe)
        return last_result
    finally:
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        if own_life is not None:
            go2.runtime.finish_life(own_life)
        if _training_behaviors.get(run_id) is entry:
            _training_behaviors.pop(run_id, None)
        if last_result.get('result') not in {'accepted', 'executing', 'timeout', 'unknown'}:
            if entry['lease_generation'] is not None:
                control_lease.release(context.session_id, generation=entry['lease_generation'])


def get_autonomy_mode() -> str:
    runtime = runtime_for(go2)
    return runtime.data['autonomy_mode'] if runtime is not None else 'manual'


def clear_activity_mode_owner() -> None:
    global _activity_mode_owner
    _activity_mode_owner = None


def get_activity_mode_owner() -> str | None:
    return _activity_mode_owner


async def set_activity_mode(mode: str, activity_id: str) -> dict:
    """Switch Go2 autonomy for a server-validated space activity.

    This in-process path never accepts a model-supplied actor. Ownership is
    checked again on release so a later direct user choice is not overwritten.
    """
    global _activity_mode_owner
    if not activity_id.startswith("act_") or mode not in {"manual", "free_explore"}:
        raise ValueError("invalid activity mode request")
    if mode == "manual":
        if _activity_mode_owner != activity_id:
            return {"ok": False, "mode": get_autonomy_mode(), "reason": "not_activity_owned"}
        _activity_mode_owner = None
        try:
            _configure_vision_consumers("manual")
        except Exception:
            logging.exception("[Go2/Activity] failed to stop autonomous consumers")
            drive.user_interrupt = True
            try:
                drive.stop()
            except Exception:
                logging.exception("[Go2/Activity] drive stop failed")
        from cloud.peers.gateway import cancel_pending
        cancel_pending(initiator_unit_id="go2", origin_kind="agent_autonomous",
                       reason="activity_ended")
        if not go2.is_connected:
            control_lease.release(activity_id)
            return {"ok": True, "mode": "manual", "physical_stop": "unknown"}
        try:
            # End the local velocity stream before handing the lease to a new activity.
            go2.stop_velocity()
        except Exception as exc:
            logging.warning("[Go2/Activity] zero velocity not confirmed: %s", exc)
        try:
            stop = await go2.send_command("StopMove", completion_timeout=5.0)
        except Exception as exc:
            logging.warning("[Go2/Activity] stop not confirmed: %s", exc)
            return {"ok": True, "mode": "manual", "physical_stop": "unknown"}
        # StopMove normally returns "accepted" after its Sport RPC response;
        # it has no completion waiter. Once the autonomous task is cancelled and
        # the stop request is accepted, its old software lease must not block
        # the next activity. A rejected RPC still keeps the lease until expiry.
        if (stop.get("result") == "ok" or
                (stop.get("result") == "accepted" and
                 stop.get("request_status_code") in (None, 0))):
            control_lease.release(activity_id)
        return {"ok": True, "mode": "manual",
                "physical_stop": "confirmed" if stop.get("result") == "ok" else "unknown"}
    if not go2.is_connected:
        return {"ok": False, "mode": get_autonomy_mode(), "reason": "offline"}
    if get_autonomy_mode() != "manual" and _activity_mode_owner != activity_id:
        return {"ok": False, "mode": get_autonomy_mode(), "reason": "mode_owned_elsewhere"}
    lease = control_lease.snapshot()
    if lease is not None and lease.owner_session_id != activity_id:
        return {"ok": False, "mode": get_autonomy_mode(), "reason": "motion_busy"}
    if _activity_mode_owner == activity_id and get_autonomy_mode() == mode:
        return {"ok": True, "mode": mode, "owner": activity_id}
    activity_context = ExecutionContext(activity_id, "visitor", False,
                                        "main", "agent_autonomous", 0)
    from cloud.space.activity import get_activity_store
    activity = get_activity_store().current()
    if (not activity or activity.get("activity_id") != activity_id
            or activity.get("status") not in {"starting", "active", "partial"}):
        return {"ok": False, "mode": get_autonomy_mode(), "reason": "activity_not_current"}
    activity_request = {"activity_id": activity_id,
                        "source_event_id": activity.get("source_event_id"),
                        "content": activity.get("context") or activity.get("text") or ""}
    _activity_mode_owner = activity_id
    try:
        _configure_vision_consumers(mode, owner_session_id=activity_id, context=activity_context,
                                    activity_request=activity_request)
    except Exception:
        _activity_mode_owner = None
        try:
            _configure_vision_consumers("manual")
        except Exception:
            logging.exception("[Go2/Activity] failed to roll back autonomous consumers")
        raise
    return {"ok": get_autonomy_mode() == mode, "mode": get_autonomy_mode(), "owner": activity_id}

# Continuous identity vision is global device activity, not an administrator
# request. It must inherit a non-persistent context. Explicit admin-triggered
# recognition still uses that request's trusted context and may be remembered.
_IDENTITY_VISION_CONTEXT = ExecutionContext(
    "go2-identity-vision", "visitor", False,
)


def _on_identity_event(event: IdentityEvent) -> None:
    person_teachings.on_identity_event(event)
    current = event.current
    previous = event.previous
    execution = current_context()
    # Fail closed for process-global callbacks. SYSTEM_CONTEXT is a convenient
    # default for trusted maintenance code, but it is not proof that an admin
    # request caused this visual event.
    may_record_episode = (
        execution.actor == "admin"
        and execution.persist
        and execution.session_id != SYSTEM_CONTEXT.session_id
    )
    identity_changed = (
        current.status == "recognized"
        and (previous.status != "recognized" or previous.person_id != current.person_id)
    )
    identity_absent = (previous.status == "recognized" and current.status == "idle"
                       and current.error_code == "face_absent")
    if identity_changed and may_record_episode:
        episode_memory.add(
            EventType.IDENTITY_CHANGE,
            f"识别到{current.name}（{current.role}，置信分 {current.score:.1f}）",
        )
    elif identity_absent and may_record_episode:
        episode_memory.add(EventType.IDENTITY_CHANGE, f"画面中已未再检测到{previous.name}的人脸")

    if identity_changed and current.role == "owner":
        from cloud.space.activity import get_activity_store
        activity = get_activity_store().current()
        if (activity and (activity.get("devices") or {}).get("go2", {}).get("status") == "joined"):
            get_activity_store().record_event(
                activity["activity_id"], "person_identified", unit_id="go2",
                text=f"已确认看到主人{current.name}", source="identity_recognition",
                confidence=current.score)

    drive.on_identity_event(event)


def _on_identity_vision_frame(frame) -> None:
    """Run the global identity frame consumer with persistence disabled.

    The identity skill creates an async task here. Context variables are copied
    into that task, so the later identity-event callback remains non-persistent.
    """
    with use_context(_IDENTITY_VISION_CONTEXT):
        identity_skill.on_vision_frame(frame)


identity_skill.add_callback(_on_identity_event)


async def _initialize_identity_vision() -> None:
    """连接后先验证人员库，再决定是否启动身份视觉消费者。"""
    await identity_skill.check_provider()
    _publish_go2_card()
    _configure_vision_consumers(get_autonomy_mode())


def _configure_vision_consumers(mode: str, owner_session_id: str = "system", context: ExecutionContext | None = None,
                                *, activity_request: dict | None = None) -> None:
    """教学与身份独立于巡游模式，只有 Drive 消费者跟随自由探索。"""
    global _active_rule_cb, _active_drive_cb, _active_identity_cb
    drive.stop()
    runtime = runtime_for(go2)
    if runtime is not None:
        runtime.commit('autonomy_config', autonomy_mode=mode)
    for callback in (_active_rule_cb, _active_drive_cb, _active_identity_cb):
        if callback is not None:
            vision_loop.remove_callback(callback)
    _active_rule_cb = _active_drive_cb = _active_identity_cb = None
    person_teachings.configure(connected=go2.is_connected, owner_session_id=owner_session_id)

    if not go2.is_connected:
        vision_loop.stop()
        identity_skill.clear_transient()
        return
    _active_rule_cb = person_teachings.on_vision_frame
    vision_loop.add_callback(_active_rule_cb)
    if identity_skill.enabled:
        _active_identity_cb = _on_identity_vision_frame
        vision_loop.add_callback(_active_identity_cb)
    if mode == "free_explore" and (runtime is None or runtime.data['activity_state'] == 'active'):
        _active_drive_cb = drive.on_vision_frame
        vision_loop.add_callback(_active_drive_cb)
        drive.start(owner_session_id=owner_session_id, context=context,
                    activity_request=activity_request)

    if any((_active_identity_cb, _active_rule_cb, _active_drive_cb)):
        vision_loop.start(lambda: go2.latest_frame_snapshot())
    else:
        vision_loop.stop()


async def prepare_user_interaction(context: ExecutionContext):
    interrupt_training_behavior(context)
    """Cancel owned motion, finish its cleanup, then observe a fresh stop."""
    person_teachings.interrupt()
    runtime = runtime_for(go2)
    if runtime is None:
        return
    from cloud.go2.navigation.navigator import navigator
    current_task = asyncio.current_task()
    drive_task = getattr(drive, '_task', None)
    nav_task = getattr(navigator, '_task', None)
    def active(task):
        return isinstance(task, asyncio.Task) and task is not current_task and not task.done()
    exploring = runtime.data['life_state'] == 'explore' or (runtime.exploration_enabled and active(drive_task))
    navigation = active(nav_task)
    if not exploring and not navigation:
        return
    owner = (runtime.data['life_owner'] if runtime.data['life_state'] == 'explore' else
             getattr(navigator, 'owner_session_id', None) if navigation else drive.owner_session_id)
    if owner not in (None, context.session_id) and context.actor != 'admin':
        raise ValueError('exploration_owned_by_another_session')
    owned_tasks = [task for task, task_owner in (
        (drive_task, getattr(drive, 'owner_session_id', None)),
        (nav_task, getattr(navigator, 'owner_session_id', None)))
        if task_owner == owner and active(task)]
    moving = (go2.body_state == 'moving' or navigation
              or getattr(go2, '_velocity_command_active', False)
              or go2._command_lock.locked())
    drive._user_handoff_in_progress = True
    try:
        if exploring:
            pause = getattr(drive, 'pause_for_user', None)
            if pause is not None:
                pause(owner_session_id=owner)
            else:
                drive.stop(owner_session_id=owner)
        navigator.stop(owner_session_id=owner)
        if owned_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*owned_tasks, return_exceptions=True), timeout=2)
            except asyncio.TimeoutError as exc:
                raise ValueError('exploration_cancellation_unconfirmed') from exc
        if moving:
            sent_at = time.monotonic()
            if not go2.pending_action and go2.body_state in {'standing', 'moving'} and go2.body_confirmation == 'observed':
                go2._motion_observation_enabled = True
            go2._stationary_samples = 0
            go2.stop_velocity()
            for _ in range(20):
                if (go2.body_state == 'standing' and go2.body_confirmation == 'observed'
                        and go2._stationary_samples >= 3
                        and (getattr(go2, '_sport_state_observed_at', None) or 0) > sent_at):
                    break
                await asyncio.sleep(.1)
            else:
                go2.runtime.body(None, confirmation='unknown')
                raise ValueError('exploration_stop_unconfirmed')
    finally:
        drive._user_handoff_in_progress = False



async def perform_life_control(action: str, context: ExecutionContext, *, request_id: str | None = None):
    """Life buttons and tools use the same confirmed teaching boundary."""
    from cloud.agents.common.effects import FileEffectLedger, current_effect, use_effect
    from cloud.go2.tasks import command_identity
    from cloud.go2.tools.executor import _training_preflight, _training_result, validate_execution_context
    import uuid
    validate_execution_context(context)
    effect = current_effect()
    own = effect is None
    if own:
        identity = command_identity(context, request_id or 'life_' + uuid.uuid4().hex)
        ledger = FileEffectLedger()
        reservation = ledger.reserve(*identity, 'LIFE', {'action': action})
        if reservation['reservation'] == 'existing':
            return {**(reservation.get('result') or {'result': 'accepted', 'error': 'command_result_unconfirmed'}),
                    'command_request_id': identity[3], 'replayed': True}
    else:
        ledger, identity = effect
    with use_effect(ledger, identity):
        checked = await _training_preflight('LIFE', {'action': action}, context)
        if checked['allowed']:
            checked = await _training_preflight('LIFE', {'action': action}, context, reserve=True)
        if not checked['allowed']:
            result = {'result': 'blocked', 'error': 'training_blocked', **_training_result(checked)}
        else:
            try:
                result = await _perform_life_control(action, context)
            except Exception:
                if own:
                    ledger.mark_result(*identity, {'result': 'error', 'error': 'life_control_failed'})
                raise
            result = {**result, **_training_result(checked),
                      'confirmation_scope': 'completion' if result.get('result') == 'ok' else 'request'}
        if own:
            result = {**result, 'command_request_id': identity[3]}
            ledger.mark_result(*identity, result)
        return result


async def _perform_life_control(action: str, context: ExecutionContext):
    from cloud.go2.state import LIFE_ACTIONS
    from cloud.go2.navigation.navigator import navigator
    if action not in LIFE_ACTIONS:
        raise ValueError('unknown life action')
    if not go2.is_connected:
        raise ValueError('Go2 未连接')
    runtime = go2.runtime
    # Validate without preempting tasks or leases: a busy rest never stops work.
    if action == 'start_explore' and (context.actor != 'admin' or not context.persist):
        return {'result': 'rejected', 'error': 'admin_only_tool_denied'}
    reason = runtime.life_reasons(navigation_busy=(navigator.mode.value != 'idle' or
             (isinstance(getattr(navigator, '_task', None), asyncio.Task) and not navigator._task.done())))[action]
    if action in {'request_rest', 'start_explore'}:
        reason = reason or go2.body_ready_reason()
    if reason:
        raise ValueError(reason)
    if action == 'stop_explore':
        owner = runtime.data['life_owner'] or drive.owner_session_id
        if owner not in (None, context.session_id) and context.actor != 'admin':
            raise ValueError('exploration_owned_by_another_session')
    if action == 'start_explore':
        snapshot, navigation_reason = navigator._snapshot()
        if snapshot is None:
            raise ValueError(navigation_reason or 'navigation_unavailable')
    if context.origin_kind == 'user_delegated':
        person_teachings.interrupt()
    acquired = control_lease.acquire(context)
    note_training_lease(context, acquired)
    if action == 'request_rest':
        runtime.request_rest(navigation_busy=(navigator.mode.value != 'idle' or
             (isinstance(getattr(navigator, '_task', None), asyncio.Task) and not navigator._task.done())))
        drive.stop()
    elif action == 'wake':
        runtime.wake()
    elif action == 'start_explore':
        global _activity_mode_owner
        _activity_mode_owner = None
        _configure_vision_consumers('free_explore', context.session_id, context)
    else:
        owner = runtime.data['life_owner'] or drive.owner_session_id
        if owner not in (None, context.session_id) and context.actor != 'admin':
            raise ValueError('exploration_owned_by_another_session')
        await prepare_user_interaction(context)
        _activity_mode_owner = None
        _configure_vision_consumers('manual')
        runtime.exploration_enabled = False
        runtime.commit('stop_explore', life_state='idle', life_owner=None, life_task_id=None)
    return {'result': 'ok', 'action': action, **go2.state_snapshot()}
