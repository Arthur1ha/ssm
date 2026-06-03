import os, json, uuid, time as _time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import paho.mqtt.publish as _mqtt_pub

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
from cloud.go2.router import router as go2_router
from contextlib import asynccontextmanager
import paho.mqtt.client as _mqtt_lib
from cloud.esp32.state import ESP32State
from cloud.esp32 import tools as esp32_tools
from cloud.esp32 import agent as esp32_agent_mod
from cloud.esp32.router import router as esp32_router

_esp32_state: ESP32State = ESP32State()
_esp32_mqtt_client = None


def _on_esp32_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe([
            ("ssm/agents/+/manifest", 0),
            ("ssm/agents/+/state",    0),
            ("ssm/agents/+/event",    0),
            ("ssm/agents/+/report",   0),
            ("ssm/result/+/+",        0),
        ])
        print("[ESP32Agent MQTT] Connected and subscribed")


def _on_esp32_message(client, userdata, msg):
    topic = msg.topic
    try:
        payload = json.loads(msg.payload.decode())
    except Exception:
        payload = {}

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
    global _esp32_mqtt_client
    broker_host = os.getenv("MQTT_BROKER_HOST", "47.116.137.202")
    broker_port = int(os.getenv("MQTT_BROKER_PORT", "1883"))

    _esp32_mqtt_client = _mqtt_lib.Client(client_id="esp32_agent", clean_session=True)
    _esp32_mqtt_client.username_pw_set(
        os.getenv("MQTT_USER", "ssm_user"),
        os.getenv("MQTT_PASSWORD", "Wl4sErQrlrpEbm7r"),
    )
    _esp32_mqtt_client.on_connect = _on_esp32_connect
    _esp32_mqtt_client.on_message = _on_esp32_message
    _esp32_mqtt_client.reconnect_delay_set(min_delay=5, max_delay=30)

    try:
        _esp32_mqtt_client.connect(broker_host, broker_port, keepalive=60)
        _esp32_mqtt_client.loop_start()
        print(f"[ESP32Agent MQTT] Connecting to {broker_host}:{broker_port}...")
    except Exception as e:
        print(f"[ESP32Agent MQTT] Connection failed: {e}")

    esp32_tools.init(_esp32_mqtt_client)
    agent = esp32_agent_mod.init(_esp32_state)
    agent.start()

    yield

    _esp32_mqtt_client.loop_stop()
    _esp32_mqtt_client.disconnect()
    print("[ESP32Agent MQTT] Disconnected")


app = FastAPI(lifespan=lifespan)
app.include_router(go2_router)
app.include_router(esp32_router)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

client = OpenAI(
    base_url=os.getenv("CHAT_API_BASE_URL", "https://tokenhub.tencentmaas.com/v1"),
    api_key=os.getenv("CHAT_API_KEY"),
)
MODEL = os.getenv("CHAT_MODEL", "hy3-preview")

NLU_SYSTEM = """你是 SSM 智能家居语音助手的意图解析器。将用户输入解析为结构化 JSON。

首先判断 intent_type：
- "execute"：立即执行的一次性指令（"把灯调暗"、"开灯"、"播放警报"）
- "define_rule"：定义自动化规则（含"以后"、"每次"、"当…就"、"检测到…就"、"自动"等词）

execute 输出格式（只输出 JSON，不要代码块和解释）：
{
  "intent_type": "execute",
  "nlu_feedback": "好的，我来帮你调暗灯光。",
  "requirements": [{"resource_tag": "lighting", "action": "dim", "context": ""}]
}

define_rule 输出格式（只输出 JSON，不要代码块和解释）：
{
  "intent_type": "define_rule",
  "nlu_feedback": "明白了，我来帮你设置这条规则。",
  "rule": {
    "name": "检测到人就开灯",
    "trigger": {"agent_tag": "presence", "event": "detected"},
    "action": {"resource_tag": "lighting", "cmd": "SET_STATE", "params": {"state": "BRIGHT"}}
  }
}

trigger.agent_tag 选择：presence（存在/红外）、light_level（光线）、sound（声音）
trigger.event 选择：
  presence → detected（检测到人）、disappeared（人离开）
  light_level → dark（变暗）、bright（变亮）
  sound → detected（检测到声音）

action.resource_tag 选择：lighting（灯光）、ambiance（氛围）、alert（警报）、notification（通知）
action.cmd 与 params：
  SET_STATE: params={state: "BRIGHT"|"DIM"|"OFF"}
  SET_COLOR: params={r:255, g:160, b:60, brightness:180}（暖光示例）
  BLINK: params={r:255, g:255, b:255, count:3}
  PLAY: params={pattern: "NOTIFY"|"ALERT"}

execute 时 resource_tag 选择：lighting、ambiance、alert、notification
execute 时 action 选择：set_color、brighten、dim、off、on、blink、alert、notify"""


_RULES_FILE   = Path(__file__).parent.parent / "orchestrator" / "rules.json"
_DEVICES_FILE = Path(__file__).parent.parent / "orchestrator" / "devices.json"


def _load_devices() -> dict:
    """读取 Orchestrator 写入的 devices.json，返回 {unit_id: manifest} dict。"""
    try:
        return json.loads(_DEVICES_FILE.read_text()) if _DEVICES_FILE.exists() else {}
    except Exception:
        return {}


def _find_device_by_slug(slug: str) -> dict | None:
    """按 slug 扫描设备注册表，未找到返回 None。"""
    for device in _load_devices().values():
        if device.get("slug") == slug:
            return device
    return None
_MQTT_HOST    = os.getenv("MQTT_BROKER_HOST", "47.116.137.202")
_MQTT_PORT    = int(os.getenv("MQTT_BROKER_PORT", "1883"))
_MQTT_USER    = os.getenv("MQTT_USER", "ssm_user")
_MQTT_PASS    = os.getenv("MQTT_PASSWORD", "Wl4sErQrlrpEbm7r")
_ESP32_AGENTS = ["esp32_desk"]  # 已知 ESP32 设备列表


def _load_rules():
    try:
        return json.loads(_RULES_FILE.read_text()) if _RULES_FILE.exists() else []
    except Exception:
        return []


def _save_rules(rules):
    _RULES_FILE.write_text(json.dumps(rules, indent=2, ensure_ascii=False))


def _push_rules_to_esp32():
    """规则变更后将精简格式规则推送到每台 ESP32（retained）。"""
    rules = _load_rules()
    esp32_rules = []
    for r in rules:
        trig = r.get("trigger", {})
        action = r.get("action", {})
        params = action.get("params", {})
        esp32_rules.append({
            "id":   r["rule_id"],
            "en":   r.get("enabled", True),
            "trig": {"tag": trig.get("agent_tag", ""), "ev": trig.get("event", "")},
            "act":  {"cmd": action.get("cmd", "SET_STATE"), **params},
        })
    payload = json.dumps(esp32_rules, ensure_ascii=False)
    try:
        for agent_id in _ESP32_AGENTS:
            _mqtt_pub.single(
                f"ssm/rules/{agent_id}", payload,
                hostname=_MQTT_HOST, port=_MQTT_PORT,
                auth={"username": _MQTT_USER, "password": _MQTT_PASS},
                retain=True, qos=1,
            )
        print(f"[API] Pushed {len(esp32_rules)} rules to ESP32")
    except Exception as e:
        print(f"[API] Rule push failed: {e}")


class NLURequest(BaseModel):
    message: str
    devices: list = []


class RuleCreateRequest(BaseModel):
    name: str
    trigger: dict
    action: dict
    enabled: bool = True


@app.post("/api/nlu")
def nlu(req: NLURequest):
    session_id = f"s_{int(_time.time())}_{uuid.uuid4().hex[:6]}"

    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=512,
        messages=[
            {"role": "system", "content": NLU_SYSTEM},
            {"role": "user",   "content": req.message},
        ]
    )

    content = response.choices[0].message.content.strip()
    try:
        result = json.loads(content)
    except Exception:
        result = {"intent_type": "execute", "nlu_feedback": "好的，我来帮你处理。", "requirements": []}

    intent_type = result.get("intent_type", "execute")

    base = {
        "session_id":   session_id,
        "intent_type":  intent_type,
        "nlu_feedback": result.get("nlu_feedback", "好的，我来处理。"),
    }
    if intent_type == "define_rule":
        base["rule"] = result.get("rule", {})
    else:
        base["requirements"] = result.get("requirements", [])

    return base


# ── 规则 CRUD ─────────────────────────────────────────────────────────

@app.get("/api/rules")
def list_rules():
    return _load_rules()


@app.post("/api/rules")
def create_rule(req: RuleCreateRequest):
    rules = _load_rules()
    rule = {
        "rule_id":    f"r_{int(_time.time())}_{uuid.uuid4().hex[:6]}",
        "name":       req.name,
        "enabled":    req.enabled,
        "trigger":    req.trigger,
        "action":     req.action,
        "created_at": int(_time.time()),
    }
    rules.append(rule)
    _save_rules(rules)
    _push_rules_to_esp32()
    return rule


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: str):
    rules = _load_rules()
    new = [r for r in rules if r["rule_id"] != rule_id]
    if len(new) == len(rules):
        return {"error": "not_found"}
    _save_rules(new)
    _push_rules_to_esp32()
    return {"deleted": rule_id}


@app.patch("/api/rules/{rule_id}/toggle")
def toggle_rule(rule_id: str, enabled: bool):
    rules = _load_rules()
    for r in rules:
        if r["rule_id"] == rule_id:
            r["enabled"] = enabled
            _save_rules(rules)
            _push_rules_to_esp32()
            return r
    return {"error": "not_found"}


# ── 设备 API ────────────────────────────────────────────────────────────────

@app.get("/api/devices")
def list_devices():
    """列出所有在线且带有 slug 的可控设备。"""
    devices = [d for d in _load_devices().values() if d.get("slug")]
    return [
        {
            "unit_id":    d.get("unit_id"),
            "name":       d.get("name"),
            "slug":       d.get("slug"),
            "agent_type": d.get("agent_type"),
            "online":     True,
        }
        for d in devices
    ]


def _go2_skills(available_actions: list[str]) -> list[dict]:
    """根据当前 FSM 状态生成机器可读的技能描述（JSON Schema）。"""
    skills = []
    sport_cmds = [a for a in available_actions
                  if a in {"StandUp", "StandDown", "Hello", "Stretch", "Dance1", "Dance2"}]
    if sport_cmds:
        skills.append({
            "id": "go2_sport",
            "description": "执行预定义动作",
            "endpoint": "/api/go2/command",
            "method": "POST",
            "inputSchema": {
                "type": "object", "required": ["cmd"],
                "properties": {"cmd": {"type": "string", "enum": sport_cmds}},
            },
        })
    if "Move" in available_actions:
        skills.append({
            "id": "go2_move",
            "description": "持续移动机器狗，发送后需调用 StopMove 停止",
            "endpoint": "/api/go2/command",
            "method": "POST",
            "inputSchema": {
                "type": "object", "required": ["cmd", "params"],
                "properties": {
                    "cmd": {"type": "string", "enum": ["Move"]},
                    "params": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "number", "description": "前后速度 m/s，正值前进"},
                            "y": {"type": "number", "description": "左右速度 m/s，正值左移"},
                            "z": {"type": "number", "description": "旋转速度 rad/s，正值左转"},
                        },
                    },
                },
            },
        })
    if "StopMove" in available_actions:
        skills.append({
            "id": "go2_stop",
            "description": "停止当前移动或动作",
            "endpoint": "/api/go2/command",
            "method": "POST",
            "inputSchema": {
                "type": "object", "required": ["cmd"],
                "properties": {"cmd": {"type": "string", "enum": ["StopMove"]}},
            },
        })
    skills.append({
        "id": "go2_chat",
        "description": "自然语言控制，可描述意图或提问，智能体自动选择工具执行",
        "endpoint": "/api/go2/chat",
        "method": "POST",
        "inputSchema": {
            "type": "object", "required": ["message"],
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "message":    {"type": "string", "description": "自然语言指令或问题"},
            },
        },
    })
    return skills


@app.get("/api/devices/{slug}/agent")
def device_agent_card(slug: str):
    """Agent Card —— 机器可读的设备能力描述，供其他 AI Agent 自动发现和调用。"""
    device = _find_device_by_slug(slug)
    if not device:
        raise HTTPException(status_code=404, detail=f"设备 '{slug}' 不存在或未上线")

    base_url = os.getenv("PUBLIC_BASE_URL", "")
    card: dict = {
        "name":          device.get("name", slug),
        "slug":          slug,
        "unit_id":       device.get("unit_id", ""),
        "agent_type":    device.get("agent_type", ""),
        "online":        True,
        "talk_to":       f"{base_url}/#/devices/{slug}",
        "capabilities":  device.get("capabilities", []),
        "resource_tags": device.get("resource_tags", []),
        "agent_tag":     device.get("agent_tag", ""),
        "ts":            device.get("ts"),
    }

    if slug == "go2":
        from cloud.go2.connection import go2 as _go2
        card["state"] = _go2.fsm_state
        card["available_actions"] = _go2.available_actions
        card["skills"] = _go2_skills(_go2.available_actions)

    return card
