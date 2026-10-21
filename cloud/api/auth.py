"""Signed cookie sessions and request authentication dependencies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import Depends, HTTPException, Request, Response

from cloud.space.registry import DEFAULT_SPACE_ID, get_space_registry

SESSION_COOKIE = "ssm_session"
CSRF_COOKIE = "ssm_csrf"
CSRF_HEADER = "X-CSRF-Token"

_rate_lock = threading.Lock()
_rate_attempts: dict[tuple[str, str], deque[float]] = defaultdict(deque)
# Keep the MVP usable without extra deployment setup. When no explicit secret
# is supplied, cookies are signed with a process-local key; restarting the API
# invalidates existing sessions, which is acceptable for the current MVP.
_EPHEMERAL_SESSION_SECRET = secrets.token_bytes(32)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _session_secret() -> bytes:
    value = os.getenv("SSM_SESSION_SECRET", "")
    if len(value) < 32:
        return _EPHEMERAL_SESSION_SECRET
    return value.encode("utf-8")


def _cookie_secure() -> bool:
    return os.getenv("SSM_COOKIE_SECURE", "true").strip().lower() not in {"0", "false", "no"}


def _ttl(actor: str) -> int:
    key = "ADMIN_SESSION_TTL_S" if actor == "admin" else "VISITOR_SESSION_TTL_S"
    default = 28800 if actor == "admin" else 7200
    try:
        return max(60, int(os.getenv(key, str(default))))
    except ValueError:
        return default


def hash_invite_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session(actor: str, invite_version: int | None = None) -> dict[str, Any]:
    now = int(time.time())
    session = {
        "session_id": "ses_" + secrets.token_urlsafe(18),
        "actor": actor,
        "space_id": DEFAULT_SPACE_ID,
        "issued_at": now,
        "expires_at": now + _ttl(actor),
        "csrf": secrets.token_urlsafe(24),
    }
    if actor == "visitor":
        session["invite_version"] = int(invite_version or 0)
    return session


def encode_session(session: dict[str, Any]) -> str:
    payload = _b64encode(json.dumps(session, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64encode(hmac.new(_session_secret(), payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def decode_session(value: str) -> dict[str, Any]:
    try:
        payload, signature = value.split(".", 1)
        expected = hmac.new(_session_secret(), payload.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64decode(signature), expected):
            raise ValueError("bad signature")
        session = json.loads(_b64decode(payload))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid session") from exc

    required = {"session_id", "actor", "space_id", "issued_at", "expires_at", "csrf"}
    if not isinstance(session, dict) or not required.issubset(session):
        raise HTTPException(status_code=401, detail="invalid session")
    if session["actor"] not in {"admin", "visitor"} or session["space_id"] != DEFAULT_SPACE_ID:
        raise HTTPException(status_code=401, detail="invalid session")
    if int(session["expires_at"]) <= int(time.time()):
        raise HTTPException(status_code=401, detail="session expired")
    if session["actor"] == "visitor":
        invite = get_space_registry().get_space()["invite"]
        if (
            not invite["enabled"]
            or int(session.get("invite_version", -1)) != int(invite["version"])
        ):
            raise HTTPException(status_code=401, detail="visitor invitation is no longer valid")
    return session


def session_is_active(session: dict[str, Any]) -> bool:
    if int(session.get("expires_at", 0)) <= int(time.time()):
        return False
    if session.get("actor") == "visitor":
        invite = get_space_registry().get_space()["invite"]
        return bool(
            invite["enabled"]
            and int(session.get("invite_version", -1)) == int(invite["version"])
        )
    return session.get("actor") == "admin"


def set_session_cookies(response: Response, session: dict[str, Any]) -> None:
    max_age = max(0, int(session["expires_at"]) - int(time.time()))
    common = {
        "secure": _cookie_secure(), "samesite": "lax", "path": "/", "max_age": max_age,
    }
    response.set_cookie(SESSION_COOKIE, encode_session(session), httponly=True, **common)
    response.set_cookie(CSRF_COOKIE, session["csrf"], httponly=False, **common)
    response.headers["Cache-Control"] = "no-store"


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=_cookie_secure(), samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=_cookie_secure(), samesite="lax")
    response.headers["Cache-Control"] = "no-store"


async def require_session(request: Request) -> dict[str, Any]:
    value = request.cookies.get(SESSION_COOKIE)
    if not value:
        raise HTTPException(status_code=401, detail="authentication required")
    session = decode_session(value)
    # Sessions are stateless cookies, so a process restart loses the in-memory
    # expiry watcher created at login.  Re-register it on every authenticated
    # request (the scheduler is idempotent) so an old, still-valid cookie
    # restores server-side cleanup without requiring another login.  Keep this
    # import local: intents depends on this module for its auth dependencies.
    from cloud.api.intents import schedule_session_cleanup

    schedule_session_cleanup(session)
    return session


async def require_csrf(
    request: Request,
    session: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    header = request.headers.get(CSRF_HEADER, "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    expected = str(session["csrf"])
    if not header or not cookie or not (
        secrets.compare_digest(header, expected) and secrets.compare_digest(cookie, expected)
    ):
        from cloud.api.policy import audit_event

        audit_event("csrf_rejected", session)
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    return session


async def require_admin(
    session: dict[str, Any] = Depends(require_session),
) -> dict[str, Any]:
    if session["actor"] != "admin":
        from cloud.api.policy import audit_event

        audit_event("permission_rejected", session, required_actor="admin")
        raise HTTPException(status_code=403, detail="administrator permission required")
    return session


async def require_admin_csrf(
    session: dict[str, Any] = Depends(require_csrf),
) -> dict[str, Any]:
    if session["actor"] != "admin":
        from cloud.api.policy import audit_event

        audit_event("permission_rejected", session, required_actor="admin")
        raise HTTPException(status_code=403, detail="administrator permission required")
    return session


def check_rate_limit(request: Request, bucket: str) -> None:
    """Small in-process limiter for credential/token exchange endpoints."""
    try:
        limit = max(1, int(os.getenv("SSM_AUTH_RATE_LIMIT_PER_MIN", "10")))
    except ValueError:
        limit = 10
    peer = request.client.host if request.client else "unknown"
    key = (bucket, peer)
    now = time.monotonic()
    with _rate_lock:
        attempts = _rate_attempts[key]
        while attempts and attempts[0] <= now - 60:
            attempts.popleft()
        if len(attempts) >= limit:
            raise HTTPException(status_code=429, detail="too many attempts")
        attempts.append(now)


def public_session(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session["session_id"],
        "actor": session["actor"],
        "space_id": DEFAULT_SPACE_ID,
        "issued_at": session["issued_at"],
        "expires_at": session["expires_at"],
        # Double-submit CSRF token is intentionally readable by this origin's
        # JavaScript; the signed session cookie remains HttpOnly.
        "csrf_token": session["csrf"],
    }
