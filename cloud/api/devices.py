import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()

_DEVICES_FILE = Path(__file__).parent.parent / "orchestrator" / "devices.json"


def _load_devices() -> dict:
    try:
        return json.loads(_DEVICES_FILE.read_text()) if _DEVICES_FILE.exists() else {}
    except Exception:
        return {}


def _find_device_by_slug(slug: str) -> dict | None:
    for device in _load_devices().values():
        if device.get("slug") == slug:
            return device
    return None


def _go2_skills(available_actions: list[str], base_url: str) -> list[dict]:
    skills = []
    sport_cmds = [a for a in available_actions
                  if a in {"StandUp", "StandDown", "Hello", "Stretch", "Dance1", "Dance2"}]
    if sport_cmds:
        skills.append({
            "id": "go2_sport",
            "description": "执行预定义动作",
            "endpoint": f"{base_url}/api/go2/commands",
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
            "endpoint": f"{base_url}/api/go2/commands",
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
            "endpoint": f"{base_url}/api/go2/commands",
            "method": "POST",
            "inputSchema": {
                "type": "object", "required": ["cmd"],
                "properties": {"cmd": {"type": "string", "enum": ["StopMove"]}},
            },
        })
    return skills


@router.get("/api/devices")
def list_devices():
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


@router.get("/api/devices/{slug}/agent")
def device_agent_card(slug: str):
    """Agent Card —— 机器可读的设备能力描述，供其他 AI Agent 自动发现和调用。"""
    base_url = os.getenv("PUBLIC_BASE_URL", "")

    if slug == "go2":
        from cloud.go2.connection import go2 as _go2
        return {
            "name":              "Go2 机器狗",
            "slug":              "go2",
            "unit_id":           "go2",
            "agent_type":        "robot",
            "online":            _go2.is_connected,
            "talk_to":           f"{base_url}/#/devices/go2",
            "capabilities":      ["sport", "move", "vision", "rules", "chat"],
            "state":             _go2.fsm_state,
            "available_actions": _go2.available_actions,
            "skills":            _go2_skills(_go2.available_actions, base_url),
        }

    device = _find_device_by_slug(slug)
    if not device:
        raise HTTPException(status_code=404, detail=f"设备 '{slug}' 不存在或未上线")

    return {
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
