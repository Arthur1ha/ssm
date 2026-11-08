"""Go2 behavior and mode side effects, shared by HTTP and model tools.

The connection runtime owns mode and behavior facts. This module wires vision,
navigation and user handoff; it does not infer physical completion.
"""
import asyncio
import logging
import time

from cloud.go2.state import runtime_for
from cloud.go2.connection import go2
from cloud.go2.mode.free_explore import drive
from cloud.go2.perception.vision import vision_loop
from cloud.go2.perception.identity import IdentityEvent, identity_skill
from cloud.agents.go2.memory.episode import EventType, episode_memory
from cloud.go2.control import control_lease
from cloud.go2.execution import ExecutionContext, SYSTEM_CONTEXT, current_context, use_context
from cloud.go2.card import _publish_go2_card

_active_rule_cb = None
_active_drive_cb = None
_active_identity_cb = None
_activity_mode_owner: str | None = None


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
    _activity_mode_owner = activity_id
    activity_context = ExecutionContext(activity_id, "visitor", False,
                                        "main", "agent_autonomous", 0)
    try:
        _configure_vision_consumers(mode, owner_session_id=activity_id, context=activity_context)
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
    identity_absent = previous.status == "recognized" and current.status == "idle"
    if identity_changed and may_record_episode:
        episode_memory.add(
            EventType.IDENTITY_CHANGE,
            f"识别到{current.name}（{current.role}，置信分 {current.score:.1f}）",
        )
    elif identity_absent and may_record_episode:
        episode_memory.add(EventType.IDENTITY_CHANGE, f"{previous.name}已离开画面")

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


def _configure_vision_consumers(mode: str, owner_session_id: str = "system", context: ExecutionContext | None = None) -> None:
    """统一重配视觉消费者；身份在两种 autonomy 模式下都可保持运行。"""
    global _active_rule_cb, _active_drive_cb, _active_identity_cb
    drive.stop()
    runtime = runtime_for(go2)
    if runtime is not None:
        runtime.commit('autonomy_config', autonomy_mode=mode)
    for callback in (_active_rule_cb, _active_drive_cb, _active_identity_cb):
        if callback is not None:
            vision_loop.remove_callback(callback)
    _active_rule_cb = _active_drive_cb = _active_identity_cb = None

    if not go2.is_connected:
        vision_loop.stop()
        identity_skill.clear_transient()
        return
    if identity_skill.enabled:
        _active_identity_cb = _on_identity_vision_frame
        vision_loop.add_callback(_active_identity_cb)
    if mode == "free_explore" and (runtime is None or runtime.data['activity_state'] == 'active'):
        _active_drive_cb = drive.on_vision_frame
        vision_loop.add_callback(_active_drive_cb)
        drive.start(owner_session_id=owner_session_id, context=context)

    if any((_active_identity_cb, _active_rule_cb, _active_drive_cb)):
        vision_loop.start(lambda: go2._latest_frame)
    else:
        vision_loop.stop()


async def prepare_user_interaction(context: ExecutionContext):
    """Cancel owned motion, finish its cleanup, then observe a fresh stop."""
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
    moving = (go2.fsm_state == 'moving' or navigation
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
            if not go2.pending_action and go2.fsm_state in {'standing', 'moving'} and go2.body_confirmation == 'observed':
                go2._motion_observation_enabled = True
            go2._stationary_samples = 0
            go2.stop_velocity()
            for _ in range(20):
                if (go2.fsm_state == 'standing' and go2.body_confirmation == 'observed'
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
    control_lease.acquire(context)
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


