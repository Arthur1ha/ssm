"""Route a single validated graph tool call through existing ESP32 boundaries."""

from __future__ import annotations

from cloud.esp32.tool_executor import execute_auxiliary, execute_physical
from cloud.esp32.tool_registry import TOOL_DECLARATIONS
from cloud.esp32.physical_tasks import stable_task_id


def execute_lamp_tool(name: str, params: dict, step_id: str, *, agent, unit_id: str,
                      context, dispatch, lighting_only: bool,
                      run_id: str) -> dict:
    # Optional Pydantic fields may arrive as null; omission preserves the
    # existing device protocol's strict parameter contract.
    params = {key: value for key, value in params.items() if value is not None}
    declaration = TOOL_DECLARATIONS.get(name)
    if declaration is None or (lighting_only and not declaration.get("action")):
        return {"result": "rejected", "error": "tool_not_allowed"}
    if name == "request_peer_skill":
        params = {**params, "request_id": step_id}
    if declaration.get("action"):
        return execute_physical(
            state=agent._state, unit_id=unit_id,
            action=declaration["action"], params=params,
            context=context, dispatch=dispatch,
            source="lamp_semantic" if lighting_only else "lamp_chat",
            task_id=stable_task_id(unit_id, run_id, step_id),
        )
    return execute_auxiliary(unit_id=unit_id, tool=name, params=params, context=context)
