import json
import time

_mqtt = None


def init(mqtt_client):
    global _mqtt
    _mqtt = mqtt_client


def publish_task(device_id: str, task_id: str, action: str, params: dict, session_id: str = "auto"):
    _mqtt.publish(
        f"ssm/task/{device_id}/{task_id}",
        json.dumps({"task_id": task_id, "session_id": session_id,
                    "action": action, "params": params, "ts": int(time.time())}),
        qos=1,
    )


def publish_speech(text: str, priority: str = "normal"):
    from cloud.esp32.tts import synthesize
    payload = {"text": text, "priority": priority}
    audio_b64 = synthesize(text)
    if audio_b64:
        payload["audio"] = audio_b64
    _mqtt.publish("ssm/agents/desk/speech", json.dumps(payload, ensure_ascii=False))


def publish_led_mood(mood: str):
    _mqtt.publish("ssm/agents/desk/led_mood", json.dumps({"mood": mood}, ensure_ascii=False))


def publish_thought(text: str, unit_id: str = "esp32_desk_led"):
    """发布智能体的"心声"台词，PWA 用 ssm/agents/+/thought 通配渲染。

    topic 按 unit_id 拼接（默认灯设备 esp32_desk_led），与 Go2 的
    ssm/agents/go2/thought 行为一致。
    """
    _mqtt.publish(f"ssm/agents/{unit_id}/thought", json.dumps({"text": text}, ensure_ascii=False))


TOOL_DESCRIPTIONS = """可用工具（系统自动注入目标设备 ID）：
- set_led_state(state): 设置 LED 状态，state 必须为 BRIGHT/DIM/OFF 之一
- set_led_color(r, g, b, brightness): 设置 LED 颜色，r/g/b/brightness 均为 0-255 整数
- speak(text): 播报中文语音，text 简洁不超过两句
无需任何动作时输出 []"""


def set_led_state(device_id: str, state: str) -> str:
    """设置 LED 开关/亮度状态。"""
    task_id = f"auto_{int(time.time())}"
    publish_task(device_id, task_id, "SET_STATE", {"state": state}, "agent_auto")
    return f"SET_STATE={state}"


def set_led_color(device_id: str, r: int, g: int, b: int, brightness: int) -> str:
    """设置 LED 颜色。"""
    task_id = f"auto_{int(time.time())}"
    params = {"r": r, "g": g, "b": b, "brightness": brightness}
    publish_task(device_id, task_id, "SET_COLOR", params, "agent_auto")
    return f"SET_COLOR r={r} g={g} b={b} brightness={brightness}"


def speak(text: str) -> str:
    """播报语音。"""
    publish_speech(text)
    publish_thought(text)
    return f"speech: {text}"


TOOL_FN_MAP: dict = {
    "set_led_state": set_led_state,
    "set_led_color": set_led_color,
    "speak": speak,
}
