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


def publish_thought(text: str):
    _mqtt.publish("ssm/agents/desk/thought", json.dumps({"text": text}, ensure_ascii=False))
