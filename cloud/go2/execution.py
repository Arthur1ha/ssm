"""Trusted execution context for Go2 internal calls.

Only the API gateway/orchestrator knows the service token.  Browser supplied
identity fields are deliberately not part of any request body model; Go2
derives them exclusively from authenticated headers.
"""

from __future__ import annotations

import contextlib
import contextvars
import hmac
import os
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request


@dataclass(frozen=True)
class ExecutionContext:
    session_id: str
    actor: str
    persist: bool
    space_id: str = "main"


SYSTEM_CONTEXT = ExecutionContext("system", "admin", True)
_current_context: contextvars.ContextVar[ExecutionContext] = contextvars.ContextVar(
    "go2_execution_context", default=SYSTEM_CONTEXT
)


def current_context() -> ExecutionContext:
    return _current_context.get()


@contextlib.contextmanager
def use_context(context: ExecutionContext):
    token = _current_context.set(context)
    try:
        yield context
    finally:
        _current_context.reset(token)


def can_persist() -> bool:
    return current_context().persist


def require_persist(action: str = "write persistent Go2 state") -> None:
    if not can_persist():
        raise PermissionError(f"visitor session cannot {action}")


def require_admin(action: str = "access administrator Go2 state") -> None:
    """Final in-process guard for tools that expose long-term private state."""
    if current_context().actor != "admin":
        raise PermissionError(f"visitor session cannot {action}")


async def require_service_context(
    x_ssm_service_token: str | None = Header(default=None),
    x_ssm_session_id: str | None = Header(default=None),
    x_ssm_actor: str | None = Header(default=None),
    x_ssm_persist: str | None = Header(default=None),
    x_ssm_space_id: str = Header(default="main"),
) -> ExecutionContext:
    """Authenticate an internal call and normalize its trusted context."""
    expected = os.getenv("SSM_INTERNAL_SERVICE_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="SSM internal service token is not configured")
    if not x_ssm_service_token or not hmac.compare_digest(x_ssm_service_token, expected):
        raise HTTPException(status_code=401, detail="invalid internal service token")
    if not x_ssm_session_id or len(x_ssm_session_id) > 128:
        raise HTTPException(status_code=400, detail="invalid session context")
    if x_ssm_actor not in {"admin", "visitor"}:
        raise HTTPException(status_code=400, detail="invalid actor context")
    if x_ssm_space_id != "main":
        raise HTTPException(status_code=400, detail="invalid space context")

    requested_persist = str(x_ssm_persist or "").lower() in {"1", "true", "yes"}
    # Role is authoritative: a compromised/buggy caller cannot make a visitor
    # persistent merely by toggling one header.
    persist = x_ssm_actor == "admin" and requested_persist
    return ExecutionContext(x_ssm_session_id, x_ssm_actor, persist, "main")


async def require_authenticated_read(request: Request):
    """Accept either a signed browser session or an authenticated service call."""
    if request.headers.get("X-SSM-Service-Token"):
        return await require_service_context(
            x_ssm_service_token=request.headers.get("X-SSM-Service-Token"),
            x_ssm_session_id=request.headers.get("X-SSM-Session-Id"),
            x_ssm_actor=request.headers.get("X-SSM-Actor"),
            x_ssm_persist=request.headers.get("X-SSM-Persist"),
            x_ssm_space_id=request.headers.get("X-SSM-Space-Id", "main"),
        )
    from cloud.api.auth import require_session
    return await require_session(request)


async def require_admin_read(request: Request):
    session = await require_authenticated_read(request)
    actor = session.actor if isinstance(session, ExecutionContext) else session.get("actor")
    if actor != "admin":
        raise HTTPException(status_code=403, detail="administrator permission required")
    return session

