import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from cloud.observability import configure_logging

logging.getLogger("cloud").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

_LOG_DIR = Path(__file__).parent.parent.parent / "logs"


class _DropSnapshotLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "video/snapshot" not in record.getMessage()

import paho.mqtt.client as _mqtt_lib
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from cloud.esp32 import agent as esp32_agent_mod
from cloud.esp32 import tools as esp32_tools
from cloud.esp32.router import router as esp32_router, internal_router as esp32_internal_router
from cloud.esp32.state import ESP32State
from cloud.go2 import router as go2_router_module
from cloud.go2.router import router as go2_router
from cloud.api.rules import router as rules_router
from cloud.api.people import router as people_router
from cloud.api.devices import router as devices_router
from cloud.api.sessions import router as sessions_router
from cloud.api.activity import router as activity_router, reconcile_current_activity
from cloud.api.personality import router as personality_router
from cloud.api import intents as intents_module
from cloud.api.intents import router as intents_router
from cloud.cards.registry import get_registry
from cloud.peers import gateway as peer_gateway
from cloud.peers.messaging import AgentMessenger
from cloud.peers.inbound import handle_api_peer_message
from cloud.space.constraints import CURRENT_CONSTRAINTS_TOPIC, get_constraint_store
from cloud.space.activity import CURRENT_ACTIVITY_TOPIC, get_activity_store
from cloud.mqtt_signing import sign_message
from cloud.space.registry import DEFAULT_SPACE_ID, get_space_registry

_esp32_state: ESP32State = ESP32State()
_esp32_mqtt_client = None
_agent_messenger: AgentMessenger | None = None
_peer_status_seen: dict[str, bool] = {}


def get_mqtt_client():
    """返回 API 层共享的 MQTT 客户端，供 go2 模块 publish 使用。"""
    return _esp32_mqtt_client


def get_agent_messenger() -> AgentMessenger | None:
    return _agent_messenger


def _on_esp32_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe([
            ("ssm/agents/+/state",  0),
            ("ssm/agents/+/event",  0),
            ("ssm/result/+/+",      0),
            ("ssm/task/+/+",        0),
            ("ssm/feedback/+",      0),
            ("ssm/peer/result/+",   0),
            ("ssm/agents/+/message", 0),
            (CURRENT_CONSTRAINTS_TOPIC, 0),
            (CURRENT_ACTIVITY_TOPIC, 0),
        ])
        # card 和 manifest topic 由 CardRegistry 统一管理订阅
        get_registry().subscribe(client)
        # Card/status 表达服务发现；WebRTC 连接投影在独立 state 中。
        go2_router_module._publish_go2_card()
        go2_router_module._publish_go2_state()
        go2_router_module._publish_go2_status()
        logger.info("[ESP32Agent MQTT] Connected and subscribed")


def _on_esp32_message(client, userdata, msg):
    topic = msg.topic
    raw = msg.payload.decode()
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {}

    # CardRegistry 处理 card 和 manifest topic（更新设备注册表）
    get_registry().handle_message(topic, raw)
    get_constraint_store().handle_message(topic, raw)
    get_activity_store().handle_message(topic, raw)
    if _agent_messenger is not None and _agent_messenger.handle_message(topic, raw):
        return
    if peer_gateway.handle_peer_result(topic, raw):
        return
    intents_module.handle_mqtt_message(topic, raw)

    parts = topic.split("/")
    if len(parts) == 4 and parts[0] == "ssm" and parts[1] == "agents":
        unit_id  = parts[2]
        msg_type = parts[3]
        if msg_type == "status" and raw.strip() != "online":
            activity = get_activity_store().current()
            if activity:
                from cloud.space.registry import card_device_id
                for card in get_registry().get_all_cards().values():
                    member = card.get("unit_id")
                    state = activity.get("devices", {}).get(member)
                    if (state and state.get("status") == "joined"
                            and (member == unit_id or card_device_id(card) == unit_id)):
                        get_activity_store().set_device(
                            activity["activity_id"], member, mode=state.get("mode", ""),
                            status="offline", owner=state.get("owner", ""),
                            error="device_offline")
                        get_activity_store().activate(activity["activity_id"])
        if unit_id == "go2" and msg_type == "status":
            online = raw.strip() == "online"
            if not online:
                peer_gateway.cancel_pending(target_unit_id="go2", reason="target_offline")
            if _peer_status_seen.get(unit_id) != online:
                _peer_status_seen[unit_id] = online
                agent = esp32_agent_mod.get_agent()
                if agent and agent.get_autonomy_mode() == "reactive":
                    agent.push_sensor_event("peer_go2", {"online": online, "ts": int(time.time())})
        if unit_id == "esp32_desk" and msg_type == "status" and raw.strip() != "online":
            peer_gateway.cancel_pending(target_unit_id=os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led"),
                                        reason="target_offline")
        if unit_id == "cloud_orchestrator" and msg_type == "status" and raw.strip() != "online":
            peer_gateway.cancel_pending(target_unit_id="cloud_orchestrator", reason="target_offline")
        if msg_type == "manifest" and isinstance(payload, dict):
            _esp32_state.on_manifest(unit_id, payload)
        if msg_type in ("state", "event") and isinstance(payload, dict):
            _esp32_state.on_agent_msg(unit_id, msg_type, payload)
            if msg_type == "state" and unit_id == os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led"):
                agent = esp32_agent_mod.get_agent()
                if agent:
                    agent.push_sensor_event(unit_id, payload)
        if msg_type == "event":
            suffix = unit_id.split("_")[-1]
            if suffix in ("light", "ir", "sound"):
                agent = esp32_agent_mod.get_agent()
                if agent:
                    agent.push_sensor_event(unit_id, payload)

    elif len(parts) == 4 and parts[1] == "result":
        task_id = parts[3]
        if isinstance(payload, dict):
            _esp32_state.store_task_result(task_id, payload)
            if parts[2] == os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led"):
                intents_module.record_lamp_task_result(task_id, payload)

    elif len(parts) == 4 and parts[1] == "task":
        # 识别"用户/编排器对设备的命令"（排除灯智能体自身的 agent_auto 任务），
        # reactive 模式触发让位窗口，避免自主调光立刻翻掉用户设置。
        if (parts[2] == os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led")
                and isinstance(payload, dict)
                and payload.get("session_id") not in (None, "agent_auto", "auto")
                and payload.get("source") != "peer_autonomous"):
            agent = esp32_agent_mod.get_agent()
            if agent:
                agent.mark_user_command(parts[2])


@asynccontextmanager
async def lifespan(app):
    configure_logging("api")
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _DropSnapshotLogs) for item in access.filters):
        access.addFilter(_DropSnapshotLogs())

    global _esp32_mqtt_client, _agent_messenger
    broker_host = os.getenv("MQTT_BROKER_HOST", "127.0.0.1")
    broker_port = int(os.getenv("MQTT_BROKER_PORT", "1883"))

    _esp32_mqtt_client = _mqtt_lib.Client(client_id="esp32_agent", clean_session=True)
    _esp32_mqtt_client.username_pw_set(
        os.getenv("MQTT_API_USER") or os.getenv("MQTT_USER", ""),
        os.getenv("MQTT_API_PASSWORD") or os.getenv("MQTT_PASSWORD", ""),
    )
    _esp32_mqtt_client.on_connect = _on_esp32_connect
    _esp32_mqtt_client.on_message = _on_esp32_message
    _esp32_mqtt_client.reconnect_delay_set(min_delay=5, max_delay=30)
    go2_router_module.init_mqtt(_esp32_mqtt_client)
    intents_module.init_mqtt(_esp32_mqtt_client)
    def publish_activity(snapshot):
        signed = sign_message(CURRENT_ACTIVITY_TOPIC, snapshot)
        info = _esp32_mqtt_client.publish(CURRENT_ACTIVITY_TOPIC,
                                          json.dumps(signed, ensure_ascii=False), qos=1, retain=True)
        if getattr(info, "rc", 0) != 0:
            raise RuntimeError("failed to publish retained activity")

    get_activity_store().configure(publish=publish_activity,
                                   emit=lambda event: intents_module._events.emit_space(event["space_id"], event))
    def emit_peer_message(message):
        intents_module._events.emit_space(DEFAULT_SPACE_ID, {
            "type": "peer_message", "event_id": "evt_" + message["message_id"],
            "message_id": message["message_id"], "sender": message["sender"],
            "target": message["target"], "text": message["content"],
            "reply_to": message.get("reply_to"), "ts": time.time(),
        })

    peer_gateway.init(_esp32_mqtt_client, _esp32_state)
    _agent_messenger = AgentMessenger(
        _esp32_mqtt_client, get_registry(), get_space_registry(),
        {"go2", os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led")},
        lambda message: handle_api_peer_message(_agent_messenger, message, _esp32_state),
        task_result_lookup=_esp32_state.get_task_result,
        on_visible_message=emit_peer_message,
    )

    # LWT：api 进程意外崩溃时把 Go2 标记离线（能力卡 retained 保留，靠 online=false 表达失活，
    # 与 ESP32 的 status/LWT 模型一致）。paho 单客户端仅一个 will，故选 status 离线。
    _esp32_mqtt_client.will_set(go2_router_module.GO2_STATUS_TOPIC, "offline", retain=True, qos=1)

    try:
        _esp32_mqtt_client.connect(broker_host, broker_port, keepalive=60)
        _esp32_mqtt_client.loop_start()
        logger.info("[ESP32Agent MQTT] Connecting to %s:%s...", broker_host, broker_port)
    except Exception as e:
        logger.error("[ESP32Agent MQTT] Connection failed: %s", e)

    esp32_tools.init(_esp32_mqtt_client)
    agent = esp32_agent_mod.init(_esp32_state)
    agent.start()

    async def reconcile_activity_once():
        await asyncio.sleep(2)
        await reconcile_current_activity()

    activity_reconcile = asyncio.create_task(reconcile_activity_once())

    async def publish_go2_heartbeat():
        while True:
            await asyncio.sleep(30)
            go2_router_module._publish_go2_state()

    go2_heartbeat = asyncio.create_task(publish_go2_heartbeat())

    try:
        yield
    finally:
        activity_reconcile.cancel()
        go2_heartbeat.cancel()
        try:
            await go2_heartbeat
        except asyncio.CancelledError:
            pass

        go2_router_module._publish_go2_status(False)
        _esp32_mqtt_client.loop_stop()
        _esp32_mqtt_client.disconnect()
        intents_module.init_mqtt(None)
        get_activity_store().configure()
        peer_gateway.shutdown()
        if _agent_messenger is not None:
            _agent_messenger.close()
            _agent_messenger = None
        logger.info("[ESP32Agent MQTT] Disconnected")


app = FastAPI(lifespan=lifespan)
app.include_router(go2_router)
app.include_router(people_router)
app.include_router(esp32_router)
app.include_router(esp32_internal_router)
app.include_router(rules_router)
app.include_router(devices_router)
app.include_router(sessions_router)
app.include_router(intents_router)
app.include_router(activity_router)
app.include_router(personality_router)

_allowed_origins = [
    origin.strip() for origin in os.getenv("SSM_ALLOWED_ORIGINS", "").split(",") if origin.strip()
]
if _allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response
