# main.py — 云端编排器入口
# MQTT 事件循环：用户意图 → 编排图

import os
import json
import queue
import time

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import paho.mqtt.client as mqtt_lib

from shared_state import SharedState
from cloud.cards.registry import CardRegistry
import tools as agent_tools
from graph import build_orchestrator

BROKER_HOST   = os.getenv("MQTT_BROKER_HOST", "127.0.0.1")
BROKER_PORT   = int(os.getenv("MQTT_BROKER_PORT", "1883"))
BROKER_USER   = os.getenv("MQTT_USER", "ssm_user")
BROKER_PASSWD = os.getenv("MQTT_PASSWORD", "Wl4sErQrlrpEbm7r")
PC_AGENT_ID   = os.getenv("PC_AGENT_ID", "pc_decision")

state = SharedState()
registry = CardRegistry()   # 编排器进程自有 CardRegistry（MQTT retained 保证与 api 同源）
event_queue: queue.Queue = queue.Queue()
_connected  = False
_announced  = False


def on_connect(client, userdata, flags, rc):
    global _connected
    print(f"[MQTT] Connected (rc={rc})")
    if rc == 0:
        _connected = True


def _subscribe_and_announce(client):
    client.subscribe([
        ("ssm/agents/+/state",  0),
        ("ssm/agents/+/event",  0),
        ("ssm/agents/+/report", 0),
        ("ssm/decision/active", 0),
        ("ssm/intent/+",        0),
        ("ssm/result/+/+",      0),
    ])
    # card 和 manifest topic 由 CardRegistry 统一订阅
    registry.subscribe(client)
    client.publish("ssm/decision/active", "true", retain=True)
    client.publish(
        f"ssm/agents/{PC_AGENT_ID}/manifest",
        json.dumps({
            "unit_id":    PC_AGENT_ID,
            "agent_type": "decision",
            "name":       "llm_orchestrator",
            "hw_platform":"pc",
            "topics": {
                "manifest": f"ssm/agents/{PC_AGENT_ID}/manifest",
                "state":    f"ssm/agents/{PC_AGENT_ID}/state",
            }
        }),
        retain=False,
    )
    client.publish(f"ssm/agents/{PC_AGENT_ID}/state",
                   json.dumps({"ism": "ACTIVE", "ts": time.time()}), retain=False)
    print(f"[MQTT] Subscribed and announced as {PC_AGENT_ID}")


def on_message(client, userdata, msg):
    topic = msg.topic
    raw = msg.payload.decode()
    try:
        payload = json.loads(raw)
    except Exception:
        payload = raw

    # CardRegistry 处理 card 与 manifest topic（更新 card-driven 规划注册表）
    registry.handle_message(topic, raw)

    if topic == "ssm/decision/active":
        state.set_decision_active(payload == "true" or payload is True)
        return

    parts = topic.split("/")

    # ssm/agents/{unit_id}/{msg_type}
    if len(parts) == 4 and parts[0] == "ssm" and parts[1] == "agents":
        unit_id  = parts[2]
        msg_type = parts[3]

        if msg_type == "manifest" and isinstance(payload, dict):
            state.on_manifest(unit_id, payload)
            return

        if msg_type in ("state", "event", "report") and isinstance(payload, dict):
            state.on_agent_msg(unit_id, msg_type, payload)

        return

    # ssm/intent/{session_id} → 编排图
    if len(parts) == 3 and parts[1] == "intent":
        session_id = parts[2]
        if isinstance(payload, dict):
            print(f"[Intent] session={session_id} user_msg={payload.get('user_msg', '')[:40]}")
            client.publish(
                f"ssm/feedback/{session_id}",
                json.dumps({"stage": "planning", "text": "已收到请求，正在处理...", "session_id": session_id}),
                qos=0,
            )
            event_queue.put({"trigger": "intent", "payload": payload, "session_id": session_id})
        return

    # ssm/result/{device_id}/{task_id}
    if len(parts) == 4 and parts[1] == "result":
        task_id = parts[3]
        if isinstance(payload, dict):
            state.store_task_result(task_id, payload)
            print(f"[Result] task={task_id} result={payload.get('result')}")


def on_disconnect(client, userdata, rc):
    global _connected, _announced
    _connected = False
    _announced = False
    print(f"[MQTT] Disconnected (rc={rc}), reconnecting...")


mqtt_client = mqtt_lib.Client(client_id=PC_AGENT_ID, clean_session=True)
mqtt_client.username_pw_set(BROKER_USER, BROKER_PASSWD)
mqtt_client.will_set("ssm/decision/active", "false", retain=True)
mqtt_client.reconnect_delay_set(min_delay=5, max_delay=30)
mqtt_client.on_connect    = on_connect
mqtt_client.on_message    = on_message
mqtt_client.on_disconnect = on_disconnect

agent_tools.init(state, mqtt_client, registry)
orchestrator = build_orchestrator()

mqtt_client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
mqtt_client.loop_start()
print(f"[Main] Connecting to broker {BROKER_HOST}:{BROKER_PORT}...")
print("[Main] 编排器就绪，等待 MQTT 事件...")

while True:
    if _connected and not _announced:
        _subscribe_and_announce(mqtt_client)
        _announced = True

    try:
        event = event_queue.get(timeout=1.0)
    except queue.Empty:
        continue

    try:
        trigger = event["trigger"]

        if trigger == "intent":
            session_id = event.get("session_id", "")
            print(f"[Main] 编排图处理 session={session_id}")
            try:
                orchestrator.invoke({
                    "session_id":   session_id,
                    "user_msg":     event["payload"].get("user_msg", ""),
                    "requirements": event["payload"].get("requirements", []),
                    "planned_tasks": [],
                    "task_results":  {},
                    "response_text": "",
                    "early_exit":    False,
                })
                print("[Main] 编排图完成。")
            except Exception as e:
                print(f"[Main] 编排图异常: {e}")
            continue

    except Exception as e:
        import traceback
        print(f"[Main] 事件处理异常（已跳过）: {e}")
        traceback.print_exc()
