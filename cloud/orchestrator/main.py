# main.py — 云端编排器入口
# MQTT 事件循环：用户意图 → 编排图

import os
import json
import queue
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
logger = logging.getLogger("orchestrator.main")

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
ORCHESTRATOR_ID = os.getenv("ORCHESTRATOR_ID", "cloud_orchestrator")

state = SharedState()
registry = CardRegistry()   # 编排器进程自有 CardRegistry（MQTT retained 保证与 api 同源）
event_queue: queue.Queue = queue.Queue()
_connected  = False
_announced  = False


def on_connect(client, userdata, flags, rc):
    global _connected
    logger.info("[MQTT] Connected (rc=%s)", rc)
    if rc == 0:
        _connected = True


def _subscribe_and_announce(client):
    client.subscribe([
        ("ssm/agents/+/state",  0),
        ("ssm/agents/+/event",  0),
        ("ssm/decision/active", 0),
        ("ssm/intent/+",        0),
        ("ssm/result/+/+",      0),
    ])
    # card 和 manifest topic 由 CardRegistry 统一订阅
    registry.subscribe(client)
    client.publish("ssm/decision/active", "true", retain=True)
    client.publish(
        f"ssm/agents/{ORCHESTRATOR_ID}/manifest",
        json.dumps({
            "unit_id":    ORCHESTRATOR_ID,
            "agent_type": "decision",
            "name":       "llm_orchestrator",
            "hw_platform":"cloud",
            "topics": {
                "manifest": f"ssm/agents/{ORCHESTRATOR_ID}/manifest",
                "state":    f"ssm/agents/{ORCHESTRATOR_ID}/state",
            }
        }),
        retain=False,
    )
    client.publish(f"ssm/agents/{ORCHESTRATOR_ID}/state",
                   json.dumps({"ism": "ACTIVE", "ts": time.time()}), retain=False)
    logger.info("[MQTT] Subscribed and announced as %s", ORCHESTRATOR_ID)


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

        if msg_type in ("state", "event") and isinstance(payload, dict):
            state.on_agent_msg(unit_id, msg_type, payload)

        return

    # ssm/intent/{session_id} → 编排图
    if len(parts) == 3 and parts[1] == "intent":
        session_id = parts[2]
        if isinstance(payload, dict):
            logger.info("[Intent] session=%s | 用户: %s", session_id, payload.get("user_msg", ""))
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
            extra = f" error={payload['error']}" if payload.get("error") else ""
            logger.info("[Result] task=%s → %s%s", task_id, payload.get("result"), extra)


def on_disconnect(client, userdata, rc):
    global _connected, _announced
    _connected = False
    _announced = False
    logger.info("[MQTT] Disconnected (rc=%s), reconnecting...", rc)


mqtt_client = mqtt_lib.Client(client_id=ORCHESTRATOR_ID, clean_session=True)
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
logger.info("[Main] Connecting to broker %s:%s...", BROKER_HOST, BROKER_PORT)
logger.info("[Main] 编排器就绪，等待 MQTT 事件...")

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
            logger.info("[Main] ── 编排图启动 session=%s ──", session_id)
            try:
                orchestrator.invoke({
                    "session_id":   session_id,
                    "user_msg":     event["payload"].get("user_msg", ""),
                    "requirements": event["payload"].get("requirements", []),
                    "route":         "",
                    "planned_tasks": [],
                    "rule":          {},
                    "task_results":  {},
                    "response_text": "",
                    "early_exit":    False,
                })
                logger.info("[Main] ── 编排图完成 session=%s ──", session_id)
            except Exception as e:
                logger.error("[Main] 编排图异常: %s", e)
            continue

    except Exception as e:
        import traceback
        logger.error("[Main] 事件处理异常（已跳过）: %s", e)
        traceback.print_exc()
