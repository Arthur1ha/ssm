"""Compatibility import for the shared space execution context."""

from cloud.space.execution import (
    ExecutionContext, SYSTEM_CONTEXT, can_persist, current_context,
    require_admin, require_admin_read, require_authenticated_read,
    require_persist, require_service_context, use_context,
)

__all__ = [
    "ExecutionContext", "SYSTEM_CONTEXT", "can_persist", "current_context",
    "require_admin", "require_admin_read", "require_authenticated_read",
    "require_persist", "require_service_context", "use_context",
]
