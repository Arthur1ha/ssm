import json
import logging
import os
import time as _time
import uuid
from pathlib import Path
import threading

import paho.mqtt.publish as _mqtt_pub
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from cloud.api.auth import require_admin, require_admin_csrf
from cloud.api.policy import audit_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rules", tags=["rules"])

_RULES_FILE  = Path(__file__).parent.parent / "orchestrator" / "rules.json"
_MQTT_HOST   = os.getenv("MQTT_BROKER_HOST", "127.0.0.1")
_MQTT_PORT   = int(os.getenv("MQTT_BROKER_PORT", "1883"))
_MQTT_USER   = os.getenv("MQTT_API_USER") or os.getenv("MQTT_USER", "")
_MQTT_PASS   = os.getenv("MQTT_API_PASSWORD") or os.getenv("MQTT_PASSWORD", "")
_ESP32_AGENTS = ["esp32_desk"]
_rules_lock = threading.RLock()


def _load_rules() -> list:
    try:
        return json.loads(_RULES_FILE.read_text()) if _RULES_FILE.exists() else []
    except Exception:
        return []


def _save_rules(rules: list) -> None:
    tmp = _RULES_FILE.with_name(f".{_RULES_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rules, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, _RULES_FILE)


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
async def list_rules(_session: dict = Depends(require_admin)):
    return _load_rules()


@router.post("")
async def create_rule(req: RuleCreateRequest, session: dict = Depends(require_admin_csrf)):
    with _rules_lock:
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
    audit_event("rule_created", session, rule_id=rule["rule_id"])
    return rule


@router.delete("/{rule_id}")
async def delete_rule(rule_id: str, session: dict = Depends(require_admin_csrf)):
    with _rules_lock:
        rules = _load_rules()
        new = [r for r in rules if r["rule_id"] != rule_id]
        if len(new) == len(rules):
            return {"error": "not_found"}
        _save_rules(new)
    _push_rules_to_esp32()
    audit_event("rule_deleted", session, rule_id=rule_id)
    return {"deleted": rule_id}


@router.patch("/{rule_id}")
async def update_rule(rule_id: str, req: RuleUpdateRequest, session: dict = Depends(require_admin_csrf)):
    updated = None
    with _rules_lock:
        rules = _load_rules()
        for r in rules:
            if r["rule_id"] == rule_id:
                r["enabled"] = req.enabled
                _save_rules(rules)
                updated = r
                break
    if updated is not None:
        _push_rules_to_esp32()
        audit_event("rule_updated", session, rule_id=rule_id)
        return updated
    return {"error": "not_found"}
