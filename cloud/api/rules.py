import json
import logging
import os
import time as _time
import uuid
from pathlib import Path

import paho.mqtt.publish as _mqtt_pub
from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rules", tags=["rules"])

_RULES_FILE  = Path(__file__).parent.parent / "orchestrator" / "rules.json"
_MQTT_HOST   = os.getenv("MQTT_BROKER_HOST", "127.0.0.1")
_MQTT_PORT   = int(os.getenv("MQTT_BROKER_PORT", "1883"))
_MQTT_USER   = os.getenv("MQTT_USER", "ssm_user")
_MQTT_PASS   = os.getenv("MQTT_PASSWORD", "Wl4sErQrlrpEbm7r")
_ESP32_AGENTS = ["esp32_desk"]


def _load_rules() -> list:
    try:
        return json.loads(_RULES_FILE.read_text()) if _RULES_FILE.exists() else []
    except Exception:
        return []


def _save_rules(rules: list) -> None:
    _RULES_FILE.write_text(json.dumps(rules, indent=2, ensure_ascii=False))


def _push_rules_to_esp32() -> None:
    rules = _load_rules()
    esp32_rules = []
    for r in rules:
        trig   = r.get("trigger", {})
        action = r.get("action", {})
        params = action.get("params", {})
        esp32_rules.append({
            "id":   r["rule_id"],
            "en":   r.get("enabled", True),
            "trig": {"tag": trig.get("tag", ""), "ev": trig.get("event", "")},
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
        logger.info("已推送 %d 条规则到 ESP32", len(esp32_rules))
    except Exception as e:
        logger.warning("规则推送失败: %s", e)


class RuleCreateRequest(BaseModel):
    name: str
    trigger: dict
    action: dict
    enabled: bool = True


class RuleUpdateRequest(BaseModel):
    enabled: bool


@router.get("")
def list_rules():
    return _load_rules()


@router.post("")
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


@router.delete("/{rule_id}")
def delete_rule(rule_id: str):
    rules = _load_rules()
    new = [r for r in rules if r["rule_id"] != rule_id]
    if len(new) == len(rules):
        return {"error": "not_found"}
    _save_rules(new)
    _push_rules_to_esp32()
    return {"deleted": rule_id}


@router.patch("/{rule_id}")
def update_rule(rule_id: str, req: RuleUpdateRequest):
    rules = _load_rules()
    for r in rules:
        if r["rule_id"] == rule_id:
            r["enabled"] = req.enabled
            _save_rules(rules)
            _push_rules_to_esp32()
            return r
    return {"error": "not_found"}
