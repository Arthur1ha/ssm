import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

logging.getLogger("cloud").setLevel(logging.INFO)

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
from cloud.esp32.router import router as esp32_router
from cloud.esp32.state import ESP32State
from cloud.go2 import router as go2_router_module
from cloud.go2.router import router as go2_router
from cloud.api.rules import router as rules_router
from cloud.api.devices import router as devices_router
from cloud.cards.registry import get_registry

_esp32_state: ESP32State = ESP32State()
_esp32_mqtt_client = None


def get_mqtt_client():
    """返回 API 层共享的 MQTT 客户端，供 go2 模块 publish 使用。"""
    return _esp32_mqtt_client


def _on_esp32_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe([
            ("ssm/agents/+/state",  0),
            ("ssm/agents/+/event",  0),
            ("ssm/agents/+/report", 0),
            ("ssm/result/+/+",      0),
        ])
        # card 和 manifest topic 由 CardRegistry 统一管理订阅
        get_registry().subscribe(client)
        print("[ESP32Agent MQTT] Connected and subscribed")


def _on_esp32_message(client, userdata, msg):
    topic = msg.topic
    raw = msg.payload.decode()
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {}

    # CardRegistry 处理 card 和 manifest topic（更新设备注册表）
    get_registry().handle_message(topic, raw)

    parts = topic.split("/")
    if len(parts) == 4 and parts[0] == "ssm" and parts[1] == "agents":
        unit_id  = parts[2]
        msg_type = parts[3]
        if msg_type == "manifest" and isinstance(payload, dict):
            _esp32_state.on_manifest(unit_id, payload)
        if msg_type in ("state", "event", "report") and isinstance(payload, dict):
            _esp32_state.on_agent_msg(unit_id, msg_type, payload)
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


@asynccontextmanager
async def lifespan(app):
    logging.getLogger("uvicorn.access").addFilter(_DropSnapshotLogs())

    # Go2 日志独立写 go2.log，不再冒泡到 uvicorn/api.log
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _go2_handler = logging.FileHandler(_LOG_DIR / "go2.log")
    _go2_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    ))
    _go2_logger = logging.getLogger("cloud.go2")
    _go2_logger.addHandler(_go2_handler)
    _go2_logger.propagate = False

    global _esp32_mqtt_client
    broker_host = os.getenv("MQTT_BROKER_HOST", "127.0.0.1")
    broker_port = int(os.getenv("MQTT_BROKER_PORT", "1883"))

    _esp32_mqtt_client = _mqtt_lib.Client(client_id="esp32_agent", clean_session=True)
    _esp32_mqtt_client.username_pw_set(
        os.getenv("MQTT_USER", "ssm_user"),
        os.getenv("MQTT_PASSWORD", "Wl4sErQrlrpEbm7r"),
    )
    _esp32_mqtt_client.on_connect = _on_esp32_connect
    _esp32_mqtt_client.on_message = _on_esp32_message
    _esp32_mqtt_client.reconnect_delay_set(min_delay=5, max_delay=30)

    # LWT：api 进程意外崩溃时，broker 自动清空 Go2 retained card，防止幽灵设备
    # paho 要求 will_set 在 connect() 之前调用
    _esp32_mqtt_client.will_set(go2_router_module.GO2_CARD_TOPIC, "", retain=True, qos=1)

    try:
        _esp32_mqtt_client.connect(broker_host, broker_port, keepalive=60)
        _esp32_mqtt_client.loop_start()
        print(f"[ESP32Agent MQTT] Connecting to {broker_host}:{broker_port}...")
    except Exception as e:
        print(f"[ESP32Agent MQTT] Connection failed: {e}")

    esp32_tools.init(_esp32_mqtt_client)
    go2_router_module.init_mqtt(_esp32_mqtt_client)
    agent = esp32_agent_mod.init(_esp32_state)
    agent.start()

    yield

    _esp32_mqtt_client.loop_stop()
    _esp32_mqtt_client.disconnect()
    print("[ESP32Agent MQTT] Disconnected")


app = FastAPI(lifespan=lifespan)
app.include_router(go2_router)
app.include_router(esp32_router)
app.include_router(rules_router)
app.include_router(devices_router)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
