# tools.py — 图节点和 DeskAgent 使用的 MQTT 发布辅助函数。
# Call init() once before building the graph.

import json
import time as _time

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
    """直接发布任意 MQTT 消息，供 DeskAgent 使用（speech、led_mood、thought）。"""
    _mqtt.publish(topic, json.dumps(payload, ensure_ascii=False))
