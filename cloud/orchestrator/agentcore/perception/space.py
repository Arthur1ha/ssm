"""Bounded MQTT-backed space snapshot formatting."""

import time

from cloud.space.registry import get_adopted_cards


def space_view(registry, shared_state, space_registry, space_id: str) -> dict:
    discovered = registry.get_all_cards() if registry else {}
    adopted = get_adopted_cards(discovered, space_registry, space_id)
    sensors = shared_state.sensor_snapshot() if shared_state else {}
    actuators = shared_state.actuator_snapshot() if shared_state else {}
    return {"discovered": discovered, "cards": adopted,
            "environment": _format_env_state(sensors, actuators)}

def _format_env_state(sensors: dict, actuators: dict) -> str:
    """把传感器和执行器快照格式化为可读字符串，注入 Planner prompt。"""
    lines = []
    for uid, data in list(sensors.items())[:20]:
        s = data.get("state") or data.get("event") or {}
        ts = s.get("ts")
        freshness = "新鲜" if isinstance(ts, (int, float)) and time.time() - ts < 120 else "时间未知或陈旧"
        lines.append(f"- {uid}：ISM={s.get('ism', '?')}；{freshness}")
    for uid, data in list(actuators.items())[:20]:
        s = data.get("state", {})
        ts = s.get("ts")
        freshness = "新鲜" if isinstance(ts, (int, float)) and time.time() - ts < 120 else "时间未知或陈旧"
        lines.append(f"- {uid}：ISM={s.get('ism', '?')}；{freshness}")
    return "\n".join(lines) if lines else "暂无环境数据"


