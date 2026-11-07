import json
import time
import uuid

_mqtt = None


def init(mqtt_client):
    global _mqtt
    _mqtt = mqtt_client


def publish_task(unit_id: str, task_id: str, action: str, params: dict, session_id: str = "auto"):
    """Compatibility helper for autonomous callers, through the common boundary."""
    from cloud.cards.registry import get_registry
    from cloud.esp32.service import get_agent
    from cloud.esp32.tool_executor import execute_physical
    from cloud.space.execution import ExecutionContext

    agent = get_agent()
    card = get_registry().get_card(unit_id)
    if agent is None or not card:
        raise ValueError("lamp_unavailable")

    def dispatch(uid, command, arguments, context, source, *, task_id):
        info = _mqtt.publish(
            f"ssm/task/{uid}/{task_id}",
            json.dumps({"task_id": task_id, "session_id": context.session_id,
                        "action": command, "params": arguments,
                        "actor": context.actor, "persist": False, "space_id": context.space_id,
                        "source": source, "ts": int(time.time())}), qos=1,
        )
        if getattr(info, "rc", 0) != 0:
            raise RuntimeError("mqtt_publish_failed")
        return task_id

    result = execute_physical(
        state=agent._state, unit_id=unit_id, action=action, params=params,
        context=ExecutionContext(session_id, "agent", False, origin_kind="agent_autonomous"),
        dispatch=dispatch, wait=False, task_id=task_id, card=card,
        source="lamp_reactive",
        preflight=lambda *_args: None if agent._should_act() else "autonomy_unavailable",
    )
    if result.get("result") not in {"accepted", "executing", "ok", "unknown"}:
        raise ValueError(result.get("error") or "lamp_action_rejected")
    return task_id


from cloud.esp32.tool_registry import TOOL_DESCRIPTIONS


def list_peers(unit_id: str = "esp32_desk_led") -> list[dict]:
    from cloud.peers.gateway import list_visible_peers
    return list_visible_peers(unit_id)


def request_peer_skill(target_unit_id: str, skill_id: str, params: dict,
                       unit_id: str = "esp32_desk_led", context=None,
                       origin_kind: str = "agent_autonomous",
                       request_id: str | None = None) -> dict:
    from cloud.peers.gateway import request_peer_skill as request
    from cloud.space.execution import ExecutionContext
    context = context or ExecutionContext("lamp-autonomous", "agent", False,
                                          origin_kind="agent_autonomous")
    return request(initiator_unit_id=unit_id, target_unit_id=target_unit_id,
                   skill_id=skill_id, params=params, context=context,
                   origin_kind=origin_kind, request_id=request_id)


def send_message(target: str, content: str, reply_to: str | None = None,
                 unit_id: str = "esp32_desk_led", interaction_kind: str = "message",
                 game_id: str | None = None) -> dict:
    from cloud.api.main import get_agent_messenger
    messenger = get_agent_messenger()
    if messenger is None:
        return {"result": "error", "error": "messaging_unavailable"}
    return messenger.send_message(unit_id, target, content, reply_to=reply_to,
                                  interaction_kind=interaction_kind, game_id=game_id)


def get_space_context(unit_id: str = "esp32_desk_led") -> dict:
    from cloud.api.main import _esp32_state
    from cloud.space.context import get_space_context as read_space
    return read_space(unit_id, state=_esp32_state)


def set_led_state(unit_id: str, state: str) -> str:
    """设置 LED 开关状态。"""
    task_id = f"auto_{uuid.uuid4().hex}"
    publish_task(unit_id, task_id, "SET_STATE", {"state": state}, "agent_auto")
    return task_id


def set_led_color(unit_id: str, r: int, g: int, b: int, brightness: int) -> str:
    """设置 LED 颜色。"""
    task_id = f"auto_{uuid.uuid4().hex}"
    params = {"r": r, "g": g, "b": b, "brightness": brightness}
    publish_task(unit_id, task_id, "SET_COLOR", params, "agent_auto")
    return task_id


def blink_led(unit_id: str, r: int, g: int, b: int, count: int) -> str:
    """闪烁 LED，设备完成并恢复原状态后才返回最终成功结果。"""
    task_id = f"auto_{uuid.uuid4().hex}"
    publish_task(unit_id, task_id, "BLINK", {"r": r, "g": g, "b": b, "count": count}, "agent_auto")
    return task_id


TOOL_FN_MAP: dict = {
    "set_led_state": set_led_state,
    "set_led_color": set_led_color,
    "blink_led": blink_led,
    "list_peers": list_peers,
    "get_space_context": get_space_context,
    "request_peer_skill": request_peer_skill,
    "send_message": send_message,
}
