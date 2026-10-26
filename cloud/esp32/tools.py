import json
import time
import uuid

_mqtt = None


def init(mqtt_client):
    global _mqtt
    _mqtt = mqtt_client


def publish_task(unit_id: str, task_id: str, action: str, params: dict, session_id: str = "auto"):
    _mqtt.publish(
        f"ssm/task/{unit_id}/{task_id}",
        json.dumps({"task_id": task_id, "session_id": session_id,
                    "action": action, "params": params,
                    "actor": "agent", "persist": False, "space_id": "main",
                    "ts": int(time.time())}),
        qos=1,
    )


def publish_speech(text: str, priority: str = "normal"):
    from cloud.esp32.tts import synthesize
    payload = {"text": text, "priority": priority}
    audio_b64 = synthesize(text)
    if audio_b64:
        payload["audio"] = audio_b64
    _mqtt.publish("ssm/agents/esp32_desk_led/speech", json.dumps(payload, ensure_ascii=False))


from cloud.esp32.agentcore.tools.registry import TOOL_DESCRIPTIONS


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
                 unit_id: str = "esp32_desk_led") -> dict:
    from cloud.api.main import get_agent_messenger
    messenger = get_agent_messenger()
    if messenger is None:
        return {"result": "error", "error": "messaging_unavailable"}
    return messenger.send_message(unit_id, target, content, reply_to=reply_to)


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


def speak(text: str) -> str:
    """播报语音。"""
    publish_speech(text)
    return f"speech: {text}"


TOOL_FN_MAP: dict = {
    "set_led_state": set_led_state,
    "set_led_color": set_led_color,
    "blink_led": blink_led,
    "speak": speak,
    "list_peers": list_peers,
    "get_space_context": get_space_context,
    "request_peer_skill": request_peer_skill,
    "send_message": send_message,
}
