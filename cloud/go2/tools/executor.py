"""Validate and execute one Go2 tool through a shared safety boundary."""

import asyncio
import inspect
import logging
import math
import time
import uuid

from cloud.agents.common.runner import invoke_blocking
from cloud.go2.state import runtime_for
from cloud.go2.connection import go2
from cloud.go2.actions import ACTIONS
from cloud.go2.control import LeaseConflict, control_lease, session_tasks
from cloud.go2.execution import ExecutionContext, current_context, use_context
from cloud.go2.tools.registry import get_tool_spec

logger = logging.getLogger(__name__)


def _audit(name: str, context: ExecutionContext, result: dict, elapsed_ms: int = 0) -> dict:
    logger.info("[Go2/ToolAudit] tool=%s actor=%s session=%s result=%s elapsed_ms=%d",
                name, context.actor, context.session_id, result["result"], elapsed_ms)
    try:
        from cloud.api.policy import audit_event
        audit_event("go2_tool_execution", {"session_id": context.session_id, "actor": context.actor},
                    tool=name, result=result["result"], error=result.get("error", ""))
    except Exception as exc:
        logger.warning("[Go2/ToolAudit] audit unavailable: %s", type(exc).__name__)
    return result


def validate_params(schema: dict, params: object) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    fields = schema["properties"]
    missing = set(schema.get("required", ())) - params.keys()
    if missing:
        raise ValueError("missing required params: " + ", ".join(sorted(missing)))
    extra = params.keys() - fields.keys()
    if extra:
        raise ValueError("unknown params: " + ", ".join(sorted(extra)))
    for key, value in params.items():
        definition = fields[key]
        kind = definition["type"]
        valid = (isinstance(value, str) if kind == "string" else
                 isinstance(value, int) and not isinstance(value, bool) if kind == "integer" else
                 isinstance(value, dict) if kind == "object" else
                 isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))
        if not valid:
            raise ValueError(f"{key} must be {kind}")
        if "enum" in definition and value not in definition["enum"]:
            raise ValueError(f"{key} must be one of {definition['enum']}")
        if kind == "string" and len(value.strip()) < definition.get("minLength", 0):
            raise ValueError(f"{key} is too short")
        if kind == "string" and "maxLength" in definition and len(value) > definition["maxLength"]:
            raise ValueError(f"{key} is too long")
        if kind in {"integer", "number"}:
            if "minimum" in definition and value < definition["minimum"]:
                raise ValueError(f"{key} is below minimum")
            if "exclusiveMinimum" in definition and value <= definition["exclusiveMinimum"]:
                raise ValueError(f"{key} is below minimum")
            if "maximum" in definition and value > definition["maximum"]:
                raise ValueError(f"{key} exceeds maximum")
    return params


def validate_execution_context(context: ExecutionContext) -> None:
    if context.actor not in {"admin", "visitor"} or not context.session_id:
        raise ValueError("invalid_execution_context")
    if context.expires_at and context.expires_at <= time.time():
        raise ValueError("execution_context_expired")


def _check_motion_owner(command: str | None, context: ExecutionContext):
    runtime = runtime_for(go2)
    if runtime is not None and context.actor != 'admin':
        owner = runtime.data.get('life_owner')
        if owner and owner != context.session_id:
            raise ValueError('interaction_owned_by_another_session')
    previous = control_lease.snapshot()
    if previous is not None and previous.owner_session_id != context.session_id:
        # Ordinary stop is session-owned, including for admins. Emergency stop
        # remains a separate low-latency, authenticated endpoint.
        if command == "StopMove" or context.actor != "admin":
            raise LeaseConflict(previous.owner_session_id, previous.expires_at)
    return previous


def check_direct_motion(command: str | None, params: dict, context: ExecutionContext) -> None:
    """Shared preflight for tools, FSM buttons, and joystick commands."""
    validate_execution_context(context)
    previous = _check_motion_owner(command, context)
    if not go2.is_connected:
        raise ValueError("Go2 未连接")
    runtime = runtime_for(go2)
    if command != 'StopMove' and runtime is not None:
        reason = runtime.active_reason() or go2.body_ready_reason()
        if reason:
            raise ValueError(reason)
    pending = getattr(go2, "pending_action", None)
    if isinstance(pending, dict):
        running = ACTIONS.get(pending.get("command"))
        if command != "StopMove":
            raise ValueError("body_action_pending_confirmation")
        if running is None or not running.interruptible:
            raise ValueError("action_interrupt_not_confirmed; use emergency stop if needed")
    if command:
        action = ACTIONS.get(command)
        if action is None:
            raise ValueError(f"Unknown command: {command}")
        extra = params.keys() - set(action.parameter_keys)
        if extra:
            raise ValueError("unknown action params: " + ", ".join(sorted(extra)))
        # The connection checks pending hardware work as well as the body graph.
        # Timed moves carry direction/duration at this layer; their velocity is
        # validated when the actual Move command is sent.
        validator = getattr(go2, "_validate_motion", None)
        if validator is not None and (command != "Move" or {"x", "y", "z"} & params.keys()):
            validator(command, params)
    if command and hasattr(go2, "fsm_next"):
        state = getattr(go2, "fsm_state", "")
        permitted = command == "StopMove" and state not in ("offline", "connecting")
        permitted = permitted or (command == "Move" and state == "moving")
        permitted = permitted or go2.fsm_next(command) is not None
        if not permitted:
            raise ValueError(f"{command} blocked in {state}")
    if command == "Move" and {"x", "y", "z"} & params.keys():
        velocity = {axis: params.get(axis, 0.0) for axis in ("x", "y", "z")}
        if hasattr(go2, "_validate_velocity"):
            go2._validate_velocity(**velocity)
    if command != "StopMove":
        control_lease.acquire(context)
        if previous is not None and previous.owner_session_id != context.session_id:
            from cloud.go2.navigation.navigator import navigator
            navigator.stop(owner_session_id=previous.owner_session_id)
            session_tasks.cancel(previous.owner_session_id)


async def execute_action(command: str, params: dict | None = None, *,
                         context: ExecutionContext | None = None,
                         life_task_id: str | None = None) -> dict:
    """The physical action boundary shared by buttons, tools and workflows.

    Sending and completion are deliberately separate. The adapter retains the
    last instant freshness, pending-action and connection-generation checks.
    """
    context = context or current_context()
    params = {} if params is None else params
    validate_execution_context(context)
    action = ACTIONS.get(command)
    if action is None:
        raise ValueError(f"Unknown command: {command}")
    if not isinstance(params, dict) or params.keys() - set(action.parameter_keys):
        raise ValueError("unknown action params")
    if not go2.is_connected:
        raise ValueError("Go2 未连接")
    _check_motion_owner(command, context)
    # Reject malformed velocity before interrupting navigation.
    if command == "Move":
        go2._validate_velocity(**{axis: params.get(axis, 0.0) for axis in ("x", "y", "z")})
    from cloud.go2.service import prepare_user_interaction, drive
    if command != "StopMove" and context.origin_kind != "agent_autonomous":
        await prepare_user_interaction(context)
    check_direct_motion(command, params, context)
    if context.origin_kind == "user_delegated" and context.expires_at > time.time():
        # This is deliberately after validation and inside the reserved call:
        # rejected requests and duplicate delivery cannot end an activity.
        from cloud.api.activity import user_intervention
        await user_intervention()
        # Activity shutdown may have changed the connection or posture while
        # we awaited cleanup; do not send using the old preflight snapshot.
        check_direct_motion(command, params, context)
    if context.origin_kind == "user_delegated" and context.session_id != "system":
        from cloud.peers.gateway import cancel_pending
        cancel_pending(initiator_unit_id="go2", origin_kind="agent_autonomous", reason="user_takeover")
    runtime = runtime_for(go2)
    token = None
    if command != "StopMove" and runtime is not None and runtime.data['life_state'] == 'idle':
        token = runtime.begin_life('play', context.session_id, life_task_id or 'command_' + uuid.uuid4().hex)
    dispatch_epoch = runtime.data['epoch'] if runtime is not None else None
    try:
        with use_context(context):
            if life_task_id is not None:
                from cloud.observability import emit_agent_event
                emit_agent_event("device.command", action=command, params=params,
                                 transport="webrtc", status="dispatching")
            raw = await go2.send_command(command, params) if params else await go2.send_command(command)
        result = normalize_result("go2_sport", raw)
        return {**result, "action": command, "device_epoch": dispatch_epoch}
    finally:
        if token is not None:
            runtime.finish_life(token)
        if context.origin_kind != "agent_autonomous":
            drive.resume_after_user()


def check_motion_preconditions(name: str, params: dict, context: ExecutionContext) -> None:
    # Navigation and timed movement require the Move source states; their own
    # tool schema has already validated goal/direction parameters.
    command = params.get("cmd") if name == "go2_sport" else "Move"
    check_direct_motion(command, {}, context)


def normalize_result(name: str, raw: object) -> dict:
    spec = get_tool_spec(name)
    if isinstance(raw, dict):
        result = dict(raw)
        status = result.get("result", "error")
        if status not in {"ok", "accepted", "executing", "blocked", "rejected", "timeout", "cancelled", "error", "needs_clarification", "disabled"}:
            result["result"] = "error"
            result["error"] = "unknown_result_status"
        return result
    if isinstance(raw, str):
        if spec and spec.result_rule == "device":
            return {"result": "error", "error": "invalid_device_result", "message": raw}
        failures = ("执行失败", "未知", "Go2 未连接", "无法", "无可用", "暂无", "不支持", "未找到")
        status = "error" if raw.startswith(failures) else "ok"
        return {"result": status, "message": raw}
    return {"result": "error", "error": "invalid_tool_result"}


async def execute_tool(name: str, params: dict | None = None, *, context: ExecutionContext | None = None) -> dict:
    context = context or current_context()
    spec = get_tool_spec(name)
    if spec is None:
        return _audit(name, context, {"result": "error", "error": "unknown_tool", "message": f"未知工具: {name}"})
    try:
        params = validate_params(spec.params_schema, params if params is not None else {})
    except ValueError as exc:
        return _audit(name, context, {"result": "rejected", "error": "invalid_params", "message": str(exc)})
    if context.actor not in spec.roles:
        return _audit(name, context, {"result": "rejected", "error": "admin_only_tool_denied"})
    if spec.persists and not context.persist:
        return _audit(name, context, {"result": "rejected", "error": "visitor_persistence_denied"})
    try:
        validate_execution_context(context)
        if spec.motion and name not in {"go2_sport", "go2_move"}:
            _check_motion_owner(None, context)
            if context.origin_kind != 'agent_autonomous':
                from cloud.go2.service import prepare_user_interaction
                await prepare_user_interaction(context)
            check_motion_preconditions(name, params, context)
        elif name != "go2_status" and not go2.is_connected and name in {"go2_observe", "go2_recognize_person", "go2_set_led"}:
            raise ValueError("Go2 未连接")
    except LeaseConflict as exc:
        return _audit(name, context, {"result": "blocked", "error": "control_lease_conflict", "owner_session_id": exc.owner_session_id})
    except ValueError as exc:
        return _audit(name, context, {"result": "blocked", "error": "device_precondition", "message": str(exc)})

    from cloud.go2.tools.tools import TOOL_FN_MAP
    fn = TOOL_FN_MAP[name]
    started = time.monotonic()
    runtime = runtime_for(go2)
    life_token = None
    if spec.motion and name not in {'go2_life', 'go2_sport'} and runtime is not None and runtime.data['life_state'] == 'idle':
        life_token = runtime.begin_life('play', context.session_id, 'tool_' + str(id(asyncio.current_task())))
    try:
        with use_context(context):
            if inspect.iscoroutinefunction(fn):
                raw = await asyncio.wait_for(fn(**params), timeout=spec.timeout_s)
            elif spec.motion:
                # A timed-out worker thread could keep moving the robot after
                # we reported a terminal result. Motion stays on this task.
                raw = fn(**params)
            else:
                raw = await asyncio.wait_for(invoke_blocking(lambda: fn(**params)), timeout=spec.timeout_s)
        result = normalize_result(name, raw)
    except asyncio.TimeoutError:
        result = {"result": "timeout", "error": "tool_timeout"}
    except asyncio.CancelledError:
        raise
    except LeaseConflict as exc:
        result = {"result": "blocked", "error": "control_lease_conflict", "owner_session_id": exc.owner_session_id}
    except ValueError as exc:
        result = {"result": "blocked", "error": "device_precondition", "message": str(exc)}
    except Exception as exc:
        logger.exception("Go2 tool %s failed", name)
        result = {"result": "error", "error": type(exc).__name__, "message": str(exc)}
    finally:
        if life_token is not None:
            runtime.finish_life(life_token)
    return _audit(name, context, result, int((time.monotonic() - started) * 1000))


async def execute_command_request(command: str, params: dict, *, context: ExecutionContext,
                                  request_id: str | None = None, trace_id: str | None = None,
                                  deadline: float | None = None) -> dict:
    """Durably reserve a direct request before entering the shared action path.

    A reservation with no result survives process loss as unconfirmed; a retry
    never converts uncertainty into permission to dispatch another action.
    """
    import re
    from hashlib import sha256
    from cloud.agents.common.effects import FileEffectLedger
    from cloud.go2.tasks import command_identity, read_command_result, track_command_result
    from cloud.observability import bind_context, emit_agent_event

    validate_execution_context(context)
    request_id = request_id or "cmd_" + uuid.uuid4().hex
    identity = command_identity(context, request_id)
    if trace_id is not None and not re.fullmatch(r"tr_[A-Fa-f0-9]{32}", trace_id):
        raise ValueError("invalid_trace_id")
    trace_id = trace_id or "tr_" + sha256("\0".join(identity).encode()).hexdigest()[:32]
    if deadline is not None and not math.isfinite(deadline):
        raise ValueError("invalid_request_deadline")
    ledger = FileEffectLedger()
    record = ledger.reserve(*identity, command, params)
    if record["reservation"] == "existing":
        previous = read_command_result(context, request_id, robot=go2)
        emit_agent_event("effect.reused", trace_id=previous.get("trace_id") or trace_id,
                         agent_id="go2", session_id=context.session_id,
                         task_id=request_id, request_id=request_id, origin_kind=context.origin_kind)
        return {"trace_id": trace_id, **previous, "replayed": True}
    runtime = runtime_for(go2)
    epoch = runtime.data['epoch'] if runtime is not None else None
    life_task_id = 'command_' + uuid.uuid4().hex

    def correlated_receipt():
        if runtime is None or runtime.data['epoch'] != epoch:
            return {}
        for item in (runtime.data.get('pending_action'), runtime.data.get('last_action')):
            if item and item.get('life_task_id') == life_task_id:
                return {'request_id': item['request_id']}
        return {}

    try:
        if deadline is not None and deadline <= time.time():
            raise ValueError("request_deadline_expired")
        with bind_context(trace_id=trace_id, agent_id="go2", session_id=context.session_id,
                          task_id=request_id, request_id=request_id, origin_kind=context.origin_kind):
            if deadline is None:
                result = await execute_action(command, params, context=context, life_task_id=life_task_id)
            else:
                result = await asyncio.wait_for(execute_action(command, params, context=context, life_task_id=life_task_id),
                                                timeout=max(0, deadline - time.time()))
    except asyncio.TimeoutError:
        result = {"result": "timeout", "error": "command_deadline_exceeded", "confirmation_scope": "request",
                  **correlated_receipt()}
    except asyncio.CancelledError:
        # Keep the durable reservation unknown. Cancellation of this waiter
        # cannot prove the device did not accept the physical command.
        track_command_result(go2, ledger, identity, {
            "result": "accepted", "error": "command_result_unconfirmed",
            "command_request_id": request_id, "trace_id": trace_id,
            "device_epoch": epoch, **correlated_receipt()})
        raise
    except LeaseConflict as exc:
        result = {"result": "blocked", "error": "control_lease_conflict",
                  "owner_session_id": exc.owner_session_id}
    except ValueError as exc:
        result = {"result": "blocked", "error": "device_precondition", "message": str(exc)}
    except Exception as exc:
        result = {"result": "error", "error": type(exc).__name__, "message": str(exc),
                  "confirmation_scope": "request"}
    result = {"device_epoch": epoch, **result, "command_request_id": request_id, "trace_id": trace_id,
              **({} if result.get("request_id") else correlated_receipt())}
    emit_agent_event("device.result", trace_id=trace_id, agent_id="go2", session_id=context.session_id,
                     task_id=request_id, request_id=result.get("request_id") or request_id,
                     origin_kind=context.origin_kind, action=command, transport="webrtc", result=result,
                     status=result["result"])
    ledger.mark_result(*identity, result)
    track_command_result(go2, ledger, identity, result)
    return read_command_result(context, request_id, robot=go2)
