"""Planner-visible Go2 tools derive from the existing device safety registry.

The registry is the sole source for names, descriptions, parameter schemas and
permissions. The Graph never calls a robot primitive directly.
"""

from cloud.go2.tools.registry import TOOL_REGISTRY, get_tool_spec
from cloud.space.execution import ExecutionContext


def tool_schemas(context: ExecutionContext | None = None) -> dict[str, dict]:
    """Expose only tools allowed by the trusted role and persistence scope."""
    declarations = {
        name: {
            "name": name,
            "description": spec.description,
            "parameters": spec.params_schema,
        }
        for name in TOOL_REGISTRY
        for spec in (get_tool_spec(name),)
        if context is None or (context.actor in spec.roles and
                               (not spec.persists or context.persist))
    }
    declarations["ask_user"] = {
        "name": "ask_user",
        "description": "缺少必要信息时暂停当前任务，向用户追问一个具体问题。",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}},
                       "required": ["question"], "additionalProperties": False},
    }
    return declarations


READ_ONLY_TOOLS = frozenset({
    "go2_status", "go2_observe", "go2_list_locations",
    "go2_recognize_person", "go2_list_people", "list_peers", "get_space_context",
})
