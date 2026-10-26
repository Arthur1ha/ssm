"""Coordinator private operations; only coordinate_request is public on its Card."""

INTERNAL_TOOLS = {
    "list_agents": {"read_only": True, "max_calls": 12},
    "read_state": {"read_only": True, "max_calls": 12},
    "invoke_skill": {"read_only": False, "max_calls": 8},
    "await_result": {"read_only": True, "max_calls": 8},
    "cancel_task": {"read_only": False, "max_calls": 8},
    "save_rule": {"read_only": False, "requires_persist": True, "max_calls": 1},
}
