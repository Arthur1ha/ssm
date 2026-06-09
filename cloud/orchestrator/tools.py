# tools.py — 编排图节点使用的 MQTT / HTTP 发布辅助函数。
# Call init() once before building the graph.

import json
import time as _time
import urllib.request

_state  = None
_mqtt   = None


def init(shared_state, mqtt_client):
    global _state, _mqtt
    _state = shared_state
    _mqtt  = mqtt_client


def do_publish_feedback(session_id: str, stage: str, text: str, status: str = "ok"):
    _mqtt.publish(
        f"ssm/feedback/{session_id}",
        json.dumps({"session_id": session_id, "stage": stage,
                    "text": text, "status": status, "ts": int(_time.time())}),
    )

def do_publish_task(device_id: str, task_id: str, action: str, params: dict, session_id: str):
    _mqtt.publish(
        f"ssm/task/{device_id}/{task_id}",
        json.dumps({"task_id": task_id, "session_id": session_id,
                    "action": action, "params": params, "ts": int(_time.time())}),
        qos=1,
    )

def do_publish(topic: str, payload: dict):
    """直接发布任意 MQTT 消息。"""
    _mqtt.publish(topic, json.dumps(payload, ensure_ascii=False))


def do_dispatch_http(url: str, payload: dict) -> dict:
    """同步 HTTP POST，用于向 HTTP 智能体（如 Go2）派发任务。"""
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[tools] HTTP dispatch 失败 ({url}): {e}")
        return {"error": str(e)}
