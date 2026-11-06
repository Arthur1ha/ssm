"""API presentation of lamp control facts; edge MQTT state stays authoritative."""

import os


def project_card(card: dict) -> dict:
    if card.get("unit_id") != os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led"):
        return card
    from cloud.esp32.service import get_agent
    agent = get_agent()
    if agent is None:
        return card
    control = agent.control_snapshot()
    return {**card, "state": {**(card.get("state") or {}), **control}}


def publish_control_change(previous: dict | None) -> dict | None:
    """Emit only a changed cloud projection, including expiration of user hold."""
    from cloud.cards.registry import get_registry
    from cloud.space.registry import get_space_registry
    unit_id = os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led")
    card = get_registry().get_card(unit_id)
    if not card:
        return previous
    configured = get_space_registry().get_space()["device_ids"]
    if unit_id not in configured and card.get("device_id") not in configured:
        return previous
    projected = project_card(card)
    state = projected.get("state") or {}
    control = {key: state.get(key) for key in
               ("autonomy_mode", "control_state", "control_hold_until", "control_ts")}
    if not control["control_ts"] or control == previous:
        return previous
    from cloud.api.intents import _events
    _events.broadcast({"type": "device", "unit_id": unit_id, "state": state})
    return control
