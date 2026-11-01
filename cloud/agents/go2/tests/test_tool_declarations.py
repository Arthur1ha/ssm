"""Go2 Graph tool visibility follows the same trusted context as execution."""

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from cloud.agents.go2.tools.declarations import tool_schemas
from cloud.go2.tools.executor import execute_tool
from cloud.go2.execution import ExecutionContext


def test_visitor_cannot_see_admin_or_persistent_tools():
    visitor = ExecutionContext("visitor-session", "visitor", False)
    names = set(tool_schemas(visitor))
    assert "go2_list_people" not in names
    assert "go2_add_rule" not in names
    assert "go2_tag_location" not in names
    assert "go2_status" in names
    assert "ask_user" in names


def test_admin_sees_full_registry_and_native_schemas():
    admin = ExecutionContext("admin-session", "admin", True)
    declarations = tool_schemas(admin)
    assert "go2_list_people" in declarations
    assert "go2_add_rule" in declarations
    for name, declaration in declarations.items():
        native = convert_to_openai_tool(declaration)
        assert native["function"]["name"] == name
        assert native["function"]["parameters"]["type"] == "object"


@pytest.mark.asyncio
async def test_executor_denies_unlisted_admin_tool_even_if_called_directly():
    visitor = ExecutionContext("visitor-session", "visitor", False)
    result = await execute_tool("go2_list_people", {}, context=visitor)
    assert result == {"result": "rejected", "error": "admin_only_tool_denied"}


@pytest.mark.asyncio
async def test_executor_rejects_invalid_motion_parameters_before_device_call():
    admin = ExecutionContext("admin-session", "admin", True)
    result = await execute_tool("go2_move", {"direction": "forward", "speed": 5}, context=admin)
    assert result["result"] == "rejected"
    assert result["error"] == "invalid_params"
