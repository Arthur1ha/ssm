"""SSM Card v1 normalization and validation at the discovery boundary."""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
_WIDGETS = {"led_color", "color_swatches", "connection", "joystick", "video"}
_VIEWS = {"light_ring", "robot_pose", "image", "generic"}
_URL_KEYS = {"endpoint", "command_endpoint", "state_stream", "status_endpoint", "get", "set"}


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Agent Card requires {label}")
    return value


def _same_origin_path(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.startswith("/api/") or value.startswith("//"):
        raise ValueError(f"{label} must be a same-origin /api/ path")
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.fragment or "\\" in value:
        raise ValueError(f"{label} must be a same-origin /api/ path")


def _same_origin_asset(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        raise ValueError(f"{label} must be a same-origin absolute path")
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.fragment or "\\" in value or ".." in parts.path.split("/"):
        raise ValueError(f"{label} must be a same-origin absolute path")


def normalize_card(payload: dict) -> dict:
    """Return a detached v0/v1 card; reject malformed v1 critical fields.

    v0 is read permissively for retained cards already in the broker. Its missing
    version means legacy, not an implicit upgrade to the current contract.
    """
    if not isinstance(payload, dict):
        raise ValueError("Agent Card must be an object")
    card = deepcopy(payload)
    version = card.get("card_schema_version", 0)
    if type(version) is not int or version not in (0, 1):
        raise ValueError(f"unsupported card_schema_version: {version!r}")
    card["unit_id"] = _required_string(card.get("unit_id") or (card.get("slug") if version == 0 else None), "unit_id")
    card.setdefault("device_id", card.get("parent_id") or card["unit_id"])
    if version == 0:
        card.setdefault("online", False)
        card.setdefault("state", {})
        card.setdefault("skills", [])
        return card

    for field in ("device_id", "name", "description", "agent_type", "version"):
        _required_string(card.get(field), field)
    transport = card.get("transport")
    if not isinstance(transport, dict) or transport.get("kind") not in ("mqtt", "http"):
        raise ValueError("transport.kind must be mqtt or http")
    if transport["kind"] == "mqtt":
        _required_string(transport.get("task_topic"), "transport.task_topic")
    else:
        _same_origin_path(transport.get("endpoint"), "transport.endpoint")
    for key in _URL_KEYS & transport.keys():
        _same_origin_path(transport[key], f"transport.{key}")
    for key, value in card.get("services", {}).items():
        if key.endswith("endpoint"):
            _same_origin_path(value, f"services.{key}")

    skills = card.get("skills")
    if not isinstance(skills, list):
        raise ValueError("skills must be an array")
    for skill in skills:
        if not isinstance(skill, dict):
            raise ValueError("skill must be an object")
        for field in ("id", "name", "description"):
            _required_string(skill.get(field), f"skill.{field}")
        if not isinstance(skill.get("tags"), list) or not isinstance(skill.get("params_schema"), dict) or not isinstance(skill.get("invoke"), dict):
            raise ValueError("skill requires tags, params_schema and invoke")
        if not isinstance(skill["invoke"].get("action"), str):
            raise ValueError("skill.invoke.action must be a string")
        override = skill["invoke"].get("transport")
        if override is not None:
            if not isinstance(override, dict) or override.get("kind") != "http":
                raise ValueError("skill.invoke.transport must be a trusted HTTP override")
            endpoint = override.get("endpoint")
            _same_origin_path(endpoint, "skill.invoke.transport.endpoint")
            # Only the service-owned lamp brain may receive a token through a
            # retained MQTT Card override. Device cards cannot route it elsewhere.
            match = re.fullmatch(r"/api/internal/esp32/([A-Za-z0-9_-]{1,64})/agent/tasks", endpoint)
            if not match or match.group(1) != card["unit_id"]:
                raise ValueError("skill.invoke.transport endpoint is not an allowed lamp service")

    sm = card.get("state_machine")
    if sm is not None:
        if not isinstance(sm, dict):
            raise ValueError("state_machine must be an object")
        _required_string(sm.get("state_field"), "state_machine.state_field")
        if "connection_field" in sm:
            _required_string(sm["connection_field"], "state_machine.connection_field")
        states = sm.get("states")
        if not isinstance(states, list) or not states or any(not isinstance(s, str) or not s for s in states) or len(set(states)) != len(states):
            raise ValueError("state_machine.states must contain unique state names")
        if not isinstance(sm.get("transitions"), list):
            raise ValueError("state_machine.transitions must be an array")
        for transition in sm["transitions"]:
            if not isinstance(transition, dict) or transition.get("src") not in states or transition.get("dst") not in states:
                raise ValueError("transition src/dst must reference state_machine.states")
            for field in ("trigger", "label"):
                _required_string(transition.get(field), f"transition.{field}")
            if "action" in transition and not isinstance(transition["action"], str):
                raise ValueError("transition.action must be a string")
            if "params" in transition and not isinstance(transition["params"], dict):
                raise ValueError("transition.params must be an object")
        if not isinstance(sm.get("views", {}), dict):
            raise ValueError("state_machine.views must be an object")
        for state, view in sm.get("views", {}).items():
            if not isinstance(view, dict) or view.get("type") not in _VIEWS:
                raise ValueError(f"unsupported state view for {state}")
            if view["type"] == "image":
                _same_origin_asset(view.get("src"), f"state_machine.views.{state}.src")

    for axis in card.get("modes", []):
        if not isinstance(axis, dict) or not isinstance(axis.get("options"), list):
            raise ValueError("mode axis requires options")
        for key in ("get", "set"):
            if key in axis:
                _same_origin_path(axis[key], f"mode.{key}")
        if "set_action" in axis:
            _required_string(axis["set_action"], "mode.set_action")

    accepted = []
    if not isinstance(card.get("widgets", []), list):
        raise ValueError("widgets must be an array")
    for widget in card.get("widgets", []):
        if not isinstance(widget, dict) or widget.get("type") not in _WIDGETS:
            logger.warning("Skipping unsupported Card widget: %r", widget)
            continue
        try:
            kind = widget["type"]
            if not isinstance(widget.get("states", []), list):
                raise ValueError("widget.states must be an array")
            if widget.get("placement", "controls") not in ("controls", "media"):
                raise ValueError("widget.placement must be controls or media")
            if kind in ("led_color", "color_swatches", "joystick"):
                _required_string(widget.get("action"), f"widget.{kind}.action")
            if kind == "connection":
                _same_origin_path(widget.get("status_endpoint"), "widget.connection.status_endpoint")
                _required_string(widget.get("connect_action"), "widget.connection.connect_action")
                _required_string(widget.get("disconnect_action"), "widget.connection.disconnect_action")
            if kind == "video":
                _same_origin_path(widget.get("endpoint"), "widget.video.endpoint")
            if sm and any(state not in sm["states"] and state not in sm.get("views", {}) for state in widget.get("states", [])):
                raise ValueError("widget.states references unknown state")
        except ValueError as exc:
            logger.warning("Skipping invalid Card widget: %s", exc)
            continue
        accepted.append(widget)
    if "widgets" in card:
        card["widgets"] = accepted
    card["online"] = False
    card["state"] = {}
    return card
