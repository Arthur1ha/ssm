"""Validate and execute one Go2 tool through a shared safety boundary."""

import asyncio
import inspect
import logging
import math
import time

from cloud.go2.connection import go2
from cloud.go2.control import LeaseConflict, control_lease
from cloud.go2.execution import ExecutionContext, current_context, use_context
from cloud.go2.agentcore.tools.registry import get_tool_spec

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


def check_direct_motion(command: str | None, params: dict, context: ExecutionContext) -> None:
    """Shared preflight for tools, FSM buttons, and joystick commands."""
    if not go2.is_connected:
        raise ValueError("Go2 未连接")
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


def check_motion_preconditions(name: str, params: dict, context: ExecutionContext) -> None:
    command = (params.get("cmd") if name == "go2_sport" else
               "Move" if name == "go2_move" else None)
    check_direct_motion(command, params, context)


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
        if spec.motion:
            check_motion_preconditions(name, params, context)
        elif name != "go2_status" and not go2.is_connected and name in {"go2_observe", "go2_recognize_person", "go2_set_led"}:
            raise ValueError("Go2 未连接")
    except LeaseConflict as exc:
        return _audit(name, context, {"result": "blocked", "error": "control_lease_conflict", "owner_session_id": exc.owner_session_id})
    except ValueError as exc:
        return _audit(name, context, {"result": "blocked", "error": "device_precondition", "message": str(exc)})

    from cloud.go2.agentcore.tools.tools import TOOL_FN_MAP
    fn = TOOL_FN_MAP[name]
    started = time.monotonic()
    try:
        with use_context(context):
            if inspect.iscoroutinefunction(fn):
                raw = await asyncio.wait_for(fn(**params), timeout=spec.timeout_s)
            elif spec.motion:
                # A timed-out worker thread could keep moving the robot after
                # we reported a terminal result. Motion stays on this task.
                raw = fn(**params)
            else:
                raw = await asyncio.wait_for(asyncio.to_thread(fn, **params), timeout=spec.timeout_s)
        result = normalize_result(name, raw)
    except asyncio.TimeoutError:
        result = {"result": "timeout", "error": "tool_timeout"}
    except asyncio.CancelledError:
        raise
    except ValueError as exc:
        result = {"result": "blocked", "error": str(exc)}
    except Exception as exc:
        logger.exception("Go2 tool %s failed", name)
        result = {"result": "error", "error": type(exc).__name__, "message": str(exc)}
    return _audit(name, context, result, int((time.monotonic() - started) * 1000))
