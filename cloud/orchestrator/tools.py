"""tools.py — 图节点使用的 MQTT/HTTP 派发与反馈辅助函数。

只做传输层操作（MQTT 发布、HTTP 调用）和状态查询，不含业务逻辑。
使用前必须先调用 init() 注入 SharedState、MQTT 客户端与 CardRegistry。
"""

import json
import os
import time as _time

import requests

_state    = None   # SharedState 实例
_mqtt     = None   # paho MQTT 客户端
_registry = None   # cloud.cards.registry.CardRegistry 实例

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


def do_publish_feedback(session_id: str, stage: str, text: str, status: str = "ok", **extra):
    """向 PWA 发布编排进度反馈（ssm/feedback/{session_id}）。

    extra 为附加字段（如 RuleBuilderNode 的 rule），会并入 payload 一同发出。
    """
    payload = {"session_id": session_id, "stage": stage,
               "text": text, "status": status, "ts": int(_time.time())}
    payload.update(extra)
    _mqtt.publish(
        f"ssm/feedback/{session_id}",
        json.dumps(payload, ensure_ascii=False),
    )


def do_publish_task(unit_id: str, task_id: str, action: str, params: dict, session_id: str):
    """向 MQTT 设备（ESP32）发布任务消息（ssm/task/{unit_id}/{task_id}）。

    topic 一律用 unit_id（传输层唯一标识），不用 slug。见 protocol/identifiers.md。
    """
    _mqtt.publish(
        f"ssm/task/{unit_id}/{task_id}",
        json.dumps({"task_id": task_id, "session_id": session_id,
                    "action": action, "params": params, "ts": int(_time.time())}),
        qos=1,
    )


def do_http_dispatch(endpoint: str, body: dict, timeout: float) -> dict:
    """向 HTTP 设备（Go2）POST 任务，返回响应 JSON（阻塞，供线程池调用）。

    endpoint 为 card.transport.endpoint（相对路径，如 /api/go2/chat），
    与 GO2_API_BASE 拼接成完整 URL。超时由调用方按 skill tag 决定。
    抛出的异常由调用方（Dispatcher）捕获并记为 timeout/error。
    """
    url = endpoint if endpoint.startswith("http") else f"{GO2_API_BASE}{endpoint}"
    # HTTP 设备走本地 API（127.0.0.1），显式绕过系统代理，
    # 否则 HTTP_PROXY 会把 localhost 请求塞进代理回环失败 → 502 Bad Gateway。
    resp = requests.post(url, json=body, timeout=timeout,
                         proxies={"http": None, "https": None})
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        return {"result": "ok", "raw": resp.text}
    if isinstance(data, dict) and "result" not in data:
        data = {**data, "result": "error" if data.get("error") else "ok"}
    return data if isinstance(data, dict) else {"result": "ok", "data": data}


def do_publish(topic: str, payload: dict):
    """直接发布任意 MQTT 消息（供 ESP32 桌面智能体复用）。"""
    _mqtt.publish(topic, json.dumps(payload, ensure_ascii=False))
