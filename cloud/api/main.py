import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

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
from cloud.api import intents as intents_module
from cloud.api.intents import router as intents_router
from cloud.cards.registry import get_registry
from cloud.peers import gateway as peer_gateway

_esp32_state: ESP32State = ESP32State()
_esp32_mqtt_client = None
_peer_status_seen: dict[str, bool] = {}


def get_mqtt_client():
    """返回 API 层共享的 MQTT 客户端，供 go2 模块 publish 使用。"""
    return _esp32_mqtt_client


def _on_esp32_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe([
            ("ssm/agents/+/state",  0),
            ("ssm/agents/+/event",  0),
            (f"ssm/agents/{os.getenv('ESP32_LED_UNIT_ID', 'esp32_desk_led')}/thought", 0),
            ("ssm/result/+/+",      0),
            ("ssm/task/+/+",        0),
            ("ssm/feedback/+",      0),
            ("ssm/peer/result/+",   0),
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
    if peer_gateway.handle_peer_result(topic, raw):
        return
    intents_module.handle_mqtt_message(topic, raw)

    parts = topic.split("/")
    if len(parts) == 4 and parts[0] == "ssm" and parts[1] == "agents":
        unit_id  = parts[2]
        msg_type = parts[3]
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
                if payload.get("source") != "lamp_chat":
                    # 设备页对话已在执行完成后回复，无需再生成一条重复的 thought。
                    # 其他命令的旁白在子线程等待结果，不阻塞 MQTT 回调。
                    action = payload.get("action")
                    params = payload.get("params", {})
                    threading.Thread(
                        target=_narrate_completed_command,
                        args=(agent, parts[3], action, params),
                        daemon=True,
                        name="ESP32Narrate",
                    ).start()


def _narrate_completed_command(agent, task_id, action, params):
    result = _esp32_state.wait_task_result(task_id, timeout=20.0)
    if result.get("result") == "ok":
        agent.narrate_command(action, params)


@asynccontextmanager
async def lifespan(app):
    logging.getLogger("uvicorn.access").addFilter(_DropSnapshotLogs())

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 规范日志格式：年月日 + 时间 + 级别 + logger 名 + 消息
    _fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # uvicorn 自带访问/错误日志默认无时间戳，统一套上规范格式
    for _uvi_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        for _h in logging.getLogger(_uvi_name).handlers:
            _h.setFormatter(_fmt)

    # Go2 日志独立写 go2.log，不再冒泡到 uvicorn/api.log
    _go2_handler = logging.FileHandler(_LOG_DIR / "go2.log")
    _go2_handler.setFormatter(_fmt)
    _go2_logger = logging.getLogger("cloud.go2")
    _go2_logger.addHandler(_go2_handler)
    _go2_logger.propagate = False

    # ESP32 日志独立写 esp32.log，不再冒泡到 uvicorn/api.log
    _esp32_handler = logging.FileHandler(_LOG_DIR / "esp32.log")
    _esp32_handler.setFormatter(_fmt)
    _esp32_logger = logging.getLogger("cloud.esp32")
    _esp32_logger.addHandler(_esp32_handler)
    _esp32_logger.propagate = False

    global _esp32_mqtt_client
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
    peer_gateway.init(_esp32_mqtt_client, _esp32_state)

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

    yield

    go2_router_module._publish_go2_status(False)
    _esp32_mqtt_client.loop_stop()
    _esp32_mqtt_client.disconnect()
    intents_module.init_mqtt(None)
    peer_gateway.shutdown()
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
