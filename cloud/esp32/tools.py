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


def publish_thought(text: str, unit_id: str = "esp32_desk_led", thought_type: str = "act"):
    """在 MQTT topic 发布智能体的"心声"台词。

    topic 按 unit_id 拼接（默认灯设备 esp32_desk_led），与 Go2 的
    ssm/agents/go2/thought 行为一致。
    """
    _mqtt.publish(f"ssm/agents/{unit_id}/thought", json.dumps({
        "text": text, "type": thought_type, "unit_id": unit_id, "ts": int(time.time()),
    }, ensure_ascii=False))


TOOL_DESCRIPTIONS = """可用工具（系统自动注入目标灯单元 ID）：
- set_led_state(state): 开关 LED，state 必须为 ON/OFF 之一；ON 恢复上次常亮配置
- set_led_color(r, g, b, brightness): 设置 LED 颜色，r/g/b/brightness 均为 0-255 整数
- blink_led(r, g, b, count): 闪烁 LED，r/g/b 为 0-255 整数，count 为 1-20
- speak(text): 播报中文语音，text 简洁不超过两句
- list_peers(): 查询同一空间的在线同伴、技能和简要状态
- request_peer_skill(target_unit_id, skill_id, params, request_id 可选): 请求同伴的公开技能；结果不是 ok 时停止后续动作。超时后用同一 request_id 查询，不重复执行。仅可请求网关允许的即时技能
无需任何动作时输出 []"""


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
    publish_thought(text)
    return f"speech: {text}"


TOOL_FN_MAP: dict = {
    "set_led_state": set_led_state,
    "set_led_color": set_led_color,
    "blink_led": blink_led,
    "speak": speak,
    "list_peers": list_peers,
    "request_peer_skill": request_peer_skill,
}
