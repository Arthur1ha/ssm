"""Normalize MQTT-backed sensor and actuator snapshots for the lamp brain."""

import datetime
import time


def sense(state, last_sound_ts: float = 0.0) -> dict:
    sensors = state.sensor_snapshot()
    light_data = None
    for unit_id, data in sensors.items():
        if unit_id.endswith("_light"):
            light_data = data.get("state") or data.get("event")
            break
    if not light_data:
        return {"light_available": False, "sound_available": any(
            unit_id.endswith("_sound") for unit_id in sensors
        )}

    now = time.time()
    sound_available = False
    sound_detected = now - last_sound_ts < 5
    for unit_id, data in sensors.items():
        if unit_id.endswith("_sound"):
            sound_available = True
            event_ts = (data.get("event") or {}).get("ts", 0)
            sound_detected = sound_detected or bool(event_ts and now - event_ts < 5)
            break

    led_state = "UNKNOWN"
    led_device_id = None
    led_config = {"r": 0, "g": 0, "b": 0, "brightness": 0, "count": None}
    for unit_id, data in state.actuator_snapshot().items():
        if unit_id.endswith("_led"):
            led_device_id = unit_id
            current = data.get("state") or {}
            led_state = current.get("ism", "UNKNOWN")
            led_config = {key: current.get(key) for key in led_config}
            break

    now_dt = datetime.datetime.now()
    hour = now_dt.hour
    period = ("深夜" if hour < 6 else "清晨" if hour < 9 else "上午" if hour < 12
              else "下午" if hour < 18 else "傍晚" if hour < 21 else "夜间")
    value = light_data.get("value", 0)
    return {
        "light_available": True, "sound_available": sound_available,
        "light_level": light_data.get("level", "NORMAL"),
        "light_value": value, "light_lux": light_data.get("lux", value),
        "sound_detected": sound_detected, "sound_recent": sound_detected,
        "led_state": led_state, "led_device_id": led_device_id,
        "led_config": led_config,
        "time_str": now_dt.strftime("%H:%M"), "time_period": period,
    }
