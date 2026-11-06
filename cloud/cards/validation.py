"""SSM Card v1 normalization and validation at the discovery boundary."""

from __future__ import annotations

import logging
import math
import re
from copy import deepcopy
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
_WIDGETS = {"led_color", "color_swatches", "connection", "joystick", "video", "map"}
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


def _validate_state_machine(sm: dict) -> None:
    _required_string(sm.get("state_field"), "state_machine.state_field")
    if "connection_field" in sm:
        _required_string(sm["connection_field"], "state_machine.connection_field")
    if "diagram_group" in sm:
        _required_string(sm["diagram_group"], "region.diagram_group")
    if "node_radius" in sm and (type(sm["node_radius"]) is not int or sm["node_radius"] <= 0):
        raise ValueError("region.node_radius must be a positive integer")
    if "diagram" in sm:
        diagram = sm["diagram"]
        if not isinstance(diagram, dict) or any(
                type(diagram.get(key)) not in (int, float) or not math.isfinite(diagram[key]) or diagram[key] <= 0
                for key in ("width", "height")):
            raise ValueError("state_machine.diagram requires positive finite dimensions")
        if not isinstance(diagram.get("sections"), list):
            raise ValueError("diagram.sections must be an array")
        for section in diagram["sections"]:
            if (not isinstance(section, dict) or type(section.get("top")) not in (int, float)
                    or not math.isfinite(section["top"]) or not 0 <= section["top"] <= diagram["height"]):
                raise ValueError("diagram section must be positioned inside the canvas")
            _required_string(section.get("label"), "diagram.section.label")
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
        for field in ("graph_hidden", "safety", "available_only"):
            if field in transition and type(transition[field]) is not bool:
                raise ValueError(f"transition.{field} must be a boolean")
        for field in ("availability_key", "completion", "description"):
            if field in transition:
                _required_string(transition[field], f"transition.{field}")
        if "required_actor" in transition and transition["required_actor"] not in ("admin", "visitor"):
            raise ValueError("transition.required_actor must be admin or visitor")
        if not transition.get("action") and any(field in transition for field in ("required_actor", "availability_key", "available_only")):
            raise ValueError("automatic transitions cannot declare action controls")
    for field in ("available_actions_field", "blocked_actions_field", "execution_field", "last_execution_field", "freshness_field", "until_field"):
        if field in sm:
            _required_string(sm[field], f"state_machine.{field}")
    if not isinstance(sm.get("views", {}), dict):
        raise ValueError("state_machine.views must be an object")
    for state, view in sm.get("views", {}).items():
        if not isinstance(view, dict) or view.get("type") not in _VIEWS:
            raise ValueError(f"unsupported state view for {state}")
        if view["type"] == "image":
            _same_origin_asset(view.get("src"), f"state_machine.views.{state}.src")

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
    observations = card.get("observations", [])
    if not isinstance(observations, list):
        raise ValueError("observations must be an array")
    for observation in observations:
        if (not isinstance(observation, dict) or
                not isinstance(observation.get("id"), str) or
                not isinstance(observation.get("fields"), dict) or
                any(value not in {"string", "boolean", "integer", "number"}
                    for value in observation["fields"].values())):
            raise ValueError("invalid observation declaration")
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
        _validate_state_machine(sm)
        if "regions" in sm:
            regions = sm["regions"]
            if not isinstance(regions, list) or not regions:
                raise ValueError("state_machine.regions must be a nonempty array")
            by_id = {}
            for region in regions:
                if not isinstance(region, dict):
                    raise ValueError("state region must be an object")
                region_id = _required_string(region.get("id"), "region.id")
                _required_string(region.get("label"), "region.label")
                if region_id in by_id:
                    raise ValueError("state region IDs must be unique")
                _validate_state_machine(region)
                if "retain_when_inactive" in region and type(region["retain_when_inactive"]) is not bool:
                    raise ValueError("region.retain_when_inactive must be a boolean")
                by_id[region_id] = region
            if "primary_region" in sm:
                _required_string(sm["primary_region"], "state_machine.primary_region")
                if sm["primary_region"] not in by_id:
                    raise ValueError("state_machine.primary_region must reference a declared region")
            for region in regions:
                visited = {region["id"]}
                current = region
                while "parent" in current:
                    parent = current["parent"]
                    if (not isinstance(parent, dict) or parent.get("region") not in by_id
                            or parent.get("state") not in by_id[parent["region"]]["states"]):
                        raise ValueError("region.parent must reference a declared region state")
                    if parent["region"] in visited:
                        raise ValueError("state region parents must not contain cycles")
                    visited.add(parent["region"])
                    current = by_id[parent["region"]]
        elif "primary_region" in sm:
            # A flat-only legacy consumer may keep this presentation hint while
            # omitting regions. It is not an instruction to synthesize topology.
            _required_string(sm["primary_region"], "state_machine.primary_region")

    for axis in card.get("modes", []):
        if not isinstance(axis, dict) or not isinstance(axis.get("options"), list):
            raise ValueError("mode axis requires options")
        for key in ("get", "set"):
            if key in axis:
                _same_origin_path(axis[key], f"mode.{key}")
        if "set_action" in axis:
            _required_string(axis["set_action"], "mode.set_action")
        if "state_field" in axis:
            _required_string(axis["state_field"], "mode.state_field")

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
            for field in ("state_field", "available_actions_field", "blocked_actions_field", "availability_key",
                          "node_state", "enter_action", "exit_action"):
                if field in widget:
                    _required_string(widget[field], f"widget.{field}")
            if "enabled_when" in widget:
                conditions = widget["enabled_when"]
                scalar = lambda value: value is None or type(value) in (str, bool, int, float)
                if (not isinstance(conditions, dict) or not conditions or
                        any(not isinstance(key, str) or not key or not (
                            scalar(value) or isinstance(value, list) and value and all(scalar(item) for item in value))
                            for key, value in conditions.items())):
                    raise ValueError("widget.enabled_when must map fields to JSON scalars or scalar arrays")
            if kind in ("led_color", "color_swatches", "joystick"):
                _required_string(widget.get("action"), f"widget.{kind}.action")
            if kind == "connection":
                _same_origin_path(widget.get("status_endpoint"), "widget.connection.status_endpoint")
                _required_string(widget.get("connect_action"), "widget.connection.connect_action")
                _required_string(widget.get("disconnect_action"), "widget.connection.disconnect_action")
            if kind in ("video", "map"):
                _same_origin_path(widget.get("endpoint"), f"widget.{kind}.endpoint")
            widget_machine = sm
            if sm and "state_field" in widget:
                widget_machine = next((region for region in sm.get("regions", [])
                                       if region["state_field"] == widget["state_field"]), None)
                if widget_machine is None and widget["state_field"] != sm["state_field"]:
                    raise ValueError("widget.state_field references an undeclared state region")
                widget_machine = widget_machine or sm
            if widget_machine and any(state not in widget_machine["states"] and state not in widget_machine.get("views", {}) for state in widget.get("states", [])):
                raise ValueError("widget.states references unknown state")
            if kind == "joystick" and "node_state" in widget:
                if not widget_machine or widget["node_state"] not in widget_machine["states"]:
                    raise ValueError("joystick.node_state references unknown state")
                for key in ("enter_action", "exit_action"):
                    _required_string(widget.get(key), f"joystick.{key}")
                    if not any(edge.get("action") == widget[key] for edge in widget_machine["transitions"]):
                        raise ValueError(f"joystick.{key} references an undeclared action")
        except ValueError as exc:
            logger.warning("Skipping invalid Card widget: %s", exc)
            continue
        accepted.append(widget)
    if "widgets" in card:
        card["widgets"] = accepted
    card["online"] = False
    card["state"] = {}
    return card
