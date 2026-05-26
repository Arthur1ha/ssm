# main.py — PC multi-agent entry point.
# Bridges MQTT events → LangGraph (Decision + Evaluation agents).
#
# Run: python main.py

import os
import json
import queue
import time

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import paho.mqtt.client as mqtt_lib

from shared_state import SharedState
import tools as agent_tools
from graph import build_orchestrator
from rule_engine import RuleEngine
from desk_agent import DeskAgent

# ── Config ────────────────────────────────────────────────────

BROKER_HOST   = os.getenv("MQTT_BROKER_HOST", "47.116.137.202")
BROKER_PORT   = int(os.getenv("MQTT_BROKER_PORT", "1883"))
BROKER_USER   = os.getenv("MQTT_USER", "ssm_user")
BROKER_PASSWD = os.getenv("MQTT_PASSWORD", "Wl4sErQrlrpEbm7r")
PC_AGENT_ID   = os.getenv("PC_AGENT_ID", "pc_decision")

# Only trigger LLM agents for these message types
TRIGGER_TYPES = {"report"}

# ── Shared state + event queue ────────────────────────────────

state = SharedState()
event_queue: queue.Queue = queue.Queue()
_connected  = False   # set True once on_connect fires; main loop uses it
_announced  = False   # set True after first subscribe+announce

# ── MQTT setup ────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    global _connected
    print(f"[MQTT] Connected (rc={rc})")
    if rc == 0:
        _connected = True   # main loop will handle subscribe + announce


def _subscribe_and_announce(client):
    """Called from main loop (not network thread) to avoid rc=7 race."""
    client.subscribe([
        ("ssm/agents/+/manifest", 0),
        ("ssm/agents/+/state",    0),
        ("ssm/agents/+/event",    0),
        ("ssm/agents/+/report",   0),
        ("ssm/decision/active",   0),
        ("ssm/intent/+",          0),
        ("ssm/result/+/+",        0),
    ])
    # 接管控制权，抑制 ESP32 本地规则
    client.publish("ssm/decision/active", "true", retain=True)
    # Announce this agent
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
    try:
        payload = json.loads(msg.payload.decode())
    except Exception:
        payload = msg.payload.decode()

    # ssm/decision/active → phone is in charge
    if topic == "ssm/decision/active":
        state.set_decision_active(payload == "true" or payload is True)
        return

    # ssm/agents/{unit_id}/{msg_type}
    parts = topic.split("/")
    if len(parts) == 4 and parts[0] == "ssm" and parts[1] == "agents":
        unit_id  = parts[2]
        msg_type = parts[3]

        if msg_type == "manifest" and isinstance(payload, dict):
            state.on_manifest(unit_id, payload)
            return

        if msg_type in ("state", "event", "report") and isinstance(payload, dict):
            state.on_agent_msg(unit_id, msg_type, payload)

        # 传感器状态变化（event）→ Decision Agent
        if msg_type == "event":
            suffix = unit_id.split("_")[-1]
            if suffix in ("light", "ir", "sound"):
                print(f"[Event] {unit_id} changed: {payload}")
                event_queue.put({"trigger": "sensor", "payload": payload, "unit_id": unit_id})

        # 执行器执行反馈（report）→ Evaluation Agent
        if msg_type == "report" and unit_id != PC_AGENT_ID:
            suffix = unit_id.split("_")[-1]
            if suffix not in ("light", "ir", "sound"):
                event_queue.put({"trigger": "actuator", "payload": payload, "unit_id": unit_id})
        return

    # ssm/intent/{session_id} → Orchestrator
    if len(parts) == 3 and parts[1] == "intent":
        session_id = parts[2]
        if isinstance(payload, dict):
            print(f"[Intent] session={session_id} user_msg={payload.get('user_msg', '')[:40]}")
            # 立即 ACK，防止 PWA 初始计时器在排队等待时超时
            client.publish(
                f"ssm/feedback/{session_id}",
                json.dumps({"stage": "planning", "text": "已收到请求，正在处理...", "session_id": session_id}),
                qos=0,
            )
            event_queue.put({"trigger": "intent", "payload": payload, "session_id": session_id})
        return

    # ssm/result/{device_id}/{task_id} → store in SharedState
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
    # 不在此处 publish — 会触发第二条重连路径与 loop_start() 冲突，造成 client_id 自踢循环
    # 断线时 "decision/active=false" 由 LWT 自动发布


mqtt_client = mqtt_lib.Client(client_id=PC_AGENT_ID, clean_session=True)
mqtt_client.username_pw_set(BROKER_USER, BROKER_PASSWD)
mqtt_client.will_set("ssm/decision/active", "false", retain=True)  # LWT：断线自动释放控制权
mqtt_client.on_connect    = on_connect
mqtt_client.on_message    = on_message
mqtt_client.on_disconnect = on_disconnect

# ── Init tools + graph ────────────────────────────────────────

agent_tools.init(state, mqtt_client)
orchestrator = build_orchestrator()
rule_engine  = RuleEngine(state, agent_tools.do_publish_task)

desk_agent = DeskAgent(state, agent_tools.do_publish_task, None)
desk_agent.start()

# ── MQTT：用 loop_start() 让 paho 自己管线程 ─────────────────

mqtt_client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
mqtt_client.loop_start()   # paho 内部 daemon 线程，Windows 兼容
print(f"[Main] Connecting to broker {BROKER_HOST}:{BROKER_PORT}...")

# ── Main loop: process events from queue ─────────────────────

print("[Main] LangGraph agents ready. Waiting for MQTT events...")

while True:
    # Subscribe + announce once after connection is confirmed (not in on_connect)
    if _connected and not _announced:
        _subscribe_and_announce(mqtt_client)
        _announced = True

    try:
        event = event_queue.get(timeout=1.0)
    except queue.Empty:
        continue

    trigger = event["trigger"]

    if trigger == "intent":
        session_id = event.get("session_id", "")
        print(f"[Main] Orchestrator invoked for session={session_id}")
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
            print("[Main] Orchestrator done.")
        except Exception as e:
            print(f"[Main] Orchestrator error: {e}")
        continue

    unit_id = event.get("unit_id", "")

    # 传感器事件：规则引擎处理，不走 LLM；同时通知 DeskAgent
    if trigger == "sensor":
        print(f"[Main] RuleEngine for unit={unit_id}")
        rule_engine.match_and_fire(unit_id, event["payload"])
        desk_agent.push_sensor_event(unit_id, event["payload"])
        continue

    # 执行器报告：V2 orchestrator 已在 evaluator_node 内联处理，此处直接跳过
    # （旧 Evaluation Agent 因 LLM 调用耗时 20-30s，会严重堵塞 intent 队列）
