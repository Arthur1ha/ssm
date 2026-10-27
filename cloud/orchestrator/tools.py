"""tools.py — 图节点使用的 MQTT/HTTP 派发与反馈辅助函数。

只做传输层操作（MQTT 发布、HTTP 调用）和状态查询，不含业务逻辑。
使用前必须先调用 init() 注入 SharedState、MQTT 客户端与 CardRegistry。
"""

import json
from cloud.observability import current_event_context, emit_agent_event
import os
import time as _time
import contextlib
import contextvars
import uuid
from urllib.parse import quote, urlparse

import requests

_state    = None   # SharedState 实例
_mqtt     = None   # paho MQTT 客户端
_registry = None   # cloud.cards.registry.CardRegistry 实例
_peer_request = contextvars.ContextVar("peer_request", default=False)

# Go2 等 HTTP 设备的 API 基址（api 进程，同机默认 8082）
GO2_API_BASE = os.getenv("GO2_API_BASE", "http://127.0.0.1:8082")


def init(shared_state, mqtt_client, registry=None):
    """注入运行时依赖。

    shared_state：线程安全设备/任务快照（ESP32 result 轮询用）。
    mqtt_client：编排器进程自己的 paho 客户端。
    registry：CardRegistry 单例，供 Planner/Dispatcher 读取 card。
    """
    global _state, _mqtt, _registry
    _state    = shared_state
    _mqtt     = mqtt_client
    _registry = registry


@contextlib.contextmanager
def peer_feedback_scope():
    """Keep internal peer coordination out of browser session feedback."""
    token = _peer_request.set(True)
    try:
        yield
    finally:
        _peer_request.reset(token)


def do_publish_feedback(session_id: str, stage: str, text: str, status: str = "ok", **extra):
    """向 PWA 发布编排进度反馈（ssm/feedback/{session_id}）。

    extra 为附加字段（如 RuleBuilderNode 的 rule），会并入 payload 一同发出。
    """
    if _peer_request.get():
        return
    if _mqtt is None:
        return
    payload = {"session_id": session_id, "stage": stage,
               "text": text, "status": status, "ts": int(_time.time())}
    payload.update(extra)
    _mqtt.publish(
        f"ssm/feedback/{session_id}",
        json.dumps(payload, ensure_ascii=False),
    )


def do_publish_ui_event(session_id: str, event_type: str, *, run_id: str,
                        trace_id: str, status: str, text: str = "", step_id: str = "",
                        stage: str = "", sequence: int = 0,
                        reply_kind: str = "final", **facts) -> None:
    if _peer_request.get():
        return
    if _mqtt is None:
        return
    payload = {"type": event_type, "agent_id": "cloud_orchestrator",
               "session_id": session_id, "source": "orchestrator",
               "run_id": run_id, "trace_id": trace_id, "status": status,
               "text": text, "step_id": step_id, "stage": stage,
               "sequence": sequence, "reply_kind": reply_kind,
               "event_id": "evt_" + uuid.uuid5(uuid.NAMESPACE_URL,
                   f"{session_id}:{run_id}:{event_type}:{stage}:{step_id}:{sequence}:{reply_kind}").hex}
    payload.update(facts)
    _mqtt.publish(f"ssm/feedback/{session_id}", json.dumps(payload, ensure_ascii=False), qos=1)


def do_publish_task(
    unit_id: str,
    task_id: str,
    action: str,
    params: dict,
    session_id: str,
    context: dict | None = None,
):
    """向 MQTT 设备（ESP32）发布任务消息（ssm/task/{unit_id}/{task_id}）。

    topic 一律用 unit_id（传输层唯一标识），不用 slug。见 protocol/identifiers.md。
    """
    if _state:
        _state.register_task(task_id, session_id)
    execution_context = context or {}
    correlation = current_event_context()
    emit_agent_event("device.command", task_id=task_id, target_unit_id=unit_id,
                     action=action, params=params, transport="mqtt", status="pending_publish")
    _mqtt.publish(
        f"ssm/task/{unit_id}/{task_id}",
        json.dumps({"task_id": task_id, "session_id": session_id,
                    "actor": execution_context.get("actor"),
                    "persist": execution_context.get("persist"),
                    "space_id": execution_context.get("space_id"),
                    "source": execution_context.get("source"),
                    "action": action, "params": params, "ts": int(_time.time()),
                    "trace_id": correlation.get("trace_id"),
                    "run_id": correlation.get("run_id")}),
        qos=1,
    )


def do_http_dispatch(endpoint: str, body: dict, timeout: float) -> dict:
    """向 HTTP 设备（Go2）POST 任务，返回响应 JSON（阻塞，供线程池调用）。

    endpoint 为 card.transport.endpoint（相对路径，如 /api/go2/chat），
    与 GO2_API_BASE 拼接成完整 URL。超时由调用方按 skill tag 决定。
    抛出的异常由调用方（Dispatcher）捕获并记为 timeout/error。
    """
    if endpoint.startswith("http"):
        target = urlparse(endpoint)
        trusted = urlparse(GO2_API_BASE)
        if (target.scheme, target.netloc) != (trusted.scheme, trusted.netloc):
            raise RuntimeError("refusing to send service token to an untrusted HTTP origin")
        url = endpoint
    else:
        url = f"{GO2_API_BASE}{endpoint}"
    # HTTP 设备走本地 API（127.0.0.1），显式绕过系统代理，
    # 否则 HTTP_PROXY 会把 localhost 请求塞进代理回环失败 → 502 Bad Gateway。
    token = os.getenv("SSM_INTERNAL_SERVICE_TOKEN", "")
    if len(token) < 16:
        raise RuntimeError("SSM_INTERNAL_SERVICE_TOKEN is not configured")
    actor = body.get("actor", "")
    persist = body.get("persist")
    headers = {
        "X-SSM-Service-Token": token,
        "X-SSM-Session-Id": str(body.get("session_id", "")),
        "X-SSM-Actor": str(actor),
        "X-SSM-Persist": "true" if persist is True else "false",
        "X-SSM-Space-Id": str(body.get("space_id", "main")),
    }
    if body.get("origin_kind") in {"user_delegated", "agent_autonomous"}:
        headers["X-SSM-Origin-Kind"] = body["origin_kind"]
        headers["X-SSM-Expires-At"] = str(int(body.get("expires_at") or 0))
    deadline = _time.monotonic() + timeout
    task_id = str(body.get("task_id") or "")
    lamp_semantic = endpoint.startswith("/api/internal/esp32/") and endpoint.endswith("/agent/tasks")
    task_query_path = (endpoint + "/" if lamp_semantic else "/api/go2/tasks/")
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=min(25, timeout),
                             allow_redirects=False,
                             proxies={"http": None, "https": None})
    except requests.exceptions.Timeout:
        if not (task_id and (endpoint == "/api/go2/chat" or lamp_semantic)):
            raise
        # The original POST may still be executing. Query its server-owned ID;
        # never resend a motion-producing request after a client timeout.
        resp = None
    if resp is not None:
        if resp.is_redirect:
            raise RuntimeError("refusing redirected internal HTTP request")
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            return {"result": "error", "error": "invalid_response"}
        if not isinstance(data, dict):
            return {"result": "error", "error": "invalid_response"}
        if data.get("result") != "executing":
            if "result" not in data:
                return {**data, "result": "error", "error": data.get("error") or "missing_execution_result"}
            return data
        if endpoint != "/api/go2/chat" and not lamp_semantic:
            return data
        task_id = str(data.get("task_id") or task_id)
    if not task_id:
        return {"result": "error", "error": "missing_task_id"}
    query_url = f"{GO2_API_BASE}{task_query_path}{quote(task_id, safe='')}"
    while _time.monotonic() < deadline:
        remaining = deadline - _time.monotonic()
        try:
            current = requests.get(query_url, headers=headers, timeout=min(10, remaining),
                                   allow_redirects=False, proxies={"http": None, "https": None})
            if current.is_redirect:
                raise RuntimeError("refusing redirected internal HTTP request")
            if current.status_code != 404:
                current.raise_for_status()
                data = current.json()
                if not isinstance(data, dict):
                    return {"result": "error", "task_id": task_id, "error": "invalid_response"}
                if data.get("result") != "executing":
                    return data
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            pass
        _time.sleep(min(1.0, max(0.0, deadline - _time.monotonic())))
    return {"result": "executing", "task_id": task_id}


def do_start_activity(context: dict, text: str, source_event_id: str,
                      user_revision: int, timeout: float = 12.0) -> dict:
    """Send one authenticated user activity request to the API's state owner."""
    token = os.getenv("SSM_INTERNAL_SERVICE_TOKEN", "")
    if len(token) < 16:
        raise RuntimeError("SSM_INTERNAL_SERVICE_TOKEN is not configured")
    headers = {
        "X-SSM-Service-Token": token,
        "X-SSM-Session-Id": str(context["session_id"]),
        "X-SSM-Actor": str(context["actor"]),
        "X-SSM-Persist": "true" if context.get("persist") is True else "false",
        "X-SSM-Space-Id": str(context["space_id"]),
        "X-SSM-Origin-Kind": "user_delegated",
        "X-SSM-Expires-At": str(int(context["expires_at"])),
    }
    response = requests.post(
        f"{GO2_API_BASE}/api/internal/activity/start",
        json={"text": text, "source_event_id": source_event_id,
              "user_revision": user_revision},
        headers=headers, timeout=timeout, allow_redirects=False,
        proxies={"http": None, "https": None},
    )
    if response.is_redirect:
        raise RuntimeError("refusing redirected internal HTTP request")
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(data.get("activity"), dict):
        raise ValueError("invalid activity start response")
    return data


def do_http_task_status(endpoint: str, task_id: str, context: dict, timeout: float = 5.0) -> dict:
    """Query an already dispatched task by its original ID; never POST it again."""
    if endpoint == "/api/go2/chat":
        path = "/api/go2/tasks/" + quote(task_id, safe="")
    elif endpoint.startswith("/api/internal/esp32/") and endpoint.endswith("/agent/tasks"):
        path = endpoint + "/" + quote(task_id, safe="")
    else:
        return {"result": "rejected", "error": "task_status_unavailable"}
    token = os.getenv("SSM_INTERNAL_SERVICE_TOKEN", "")
    if len(token) < 16:
        return {"result": "error", "error": "service_token_unavailable"}
    headers = {
        "X-SSM-Service-Token": token,
        "X-SSM-Session-Id": str(context.get("session_id", "")),
        "X-SSM-Actor": str(context.get("actor", "")),
        "X-SSM-Persist": "true" if context.get("persist") is True else "false",
        "X-SSM-Space-Id": str(context.get("space_id", "main")),
        "X-SSM-Origin-Kind": str(context.get("origin_kind") or "user_delegated"),
        "X-SSM-Expires-At": str(int(context.get("expires_at") or 0)),
    }
    try:
        response = requests.get(f"{GO2_API_BASE}{path}", headers=headers,
                                timeout=timeout, allow_redirects=False,
                                proxies={"http": None, "https": None})
        if response.is_redirect:
            return {"result": "error", "error": "redirect_denied"}
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.Timeout:
        return {"result": "timeout", "task_id": task_id}
    except Exception:
        return {"result": "error", "task_id": task_id, "error": "task_status_failed"}
    return data if isinstance(data, dict) and isinstance(data.get("result"), str) else {
        "result": "error", "task_id": task_id, "error": "invalid_response"}


def do_cleanup_go2_session(session_id: str, actor: str = "visitor") -> dict:
    """Tell Go2 to release only resources owned by one ended session."""
    endpoint = f"/api/go2/sessions/{session_id}/cleanup"
    body = {
        "session_id": session_id,
        "actor": actor,
        "persist": False,
        "space_id": "main",
    }
    return do_http_dispatch(endpoint, body, timeout=5)


def do_publish(topic: str, payload: dict):
    """直接发布任意 MQTT 消息（供 ESP32 桌面智能体复用）。"""
    _mqtt.publish(topic, json.dumps(payload, ensure_ascii=False))
