"""Lost receipts recover through queries without repeating physical actions."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from cloud.esp32 import physical_tasks
from cloud.esp32.state import ESP32State
from edge import ism, lamp_contract

UNIT = "esp32_desk_led"


@pytest.fixture
def edge_modules(monkeypatch):
    monkeypatch.setitem(sys.modules, "clock", SimpleNamespace(unix_time=lambda: 100))
    monkeypatch.setitem(sys.modules, "ism", ism)
    monkeypatch.setitem(sys.modules, "lamp_contract", lamp_contract)
    monkeypatch.setitem(sys.modules, "config", SimpleNamespace(
        DEVICE_ID="esp32_desk", UNIT_LED=UNIT, UNIT_LIGHT="light", UNIT_IR="ir",
        UNIT_SOUND="sound", MQTT_BROKER_IP="broker", MQTT_PORT=1883,
        MQTT_USER="test", MQTT_PASSWORD="test", WIFI_SSID="test", WIFI_PASSWORD="test"))

    def load(name):
        spec = importlib.util.spec_from_file_location(
            "receipt_check_" + name, Path(__file__).parents[2] / "edge" / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return load


@pytest.fixture
def bridge(edge_modules):
    calls, receipts = [], []
    driver = SimpleNamespace(led_set_state=lambda value: calls.append(value),
                             led_set_color=lambda *_args: calls.append("ON"))
    mqtt = SimpleNamespace(publish=lambda topic, payload, **kw: receipts.append((topic, payload, kw)))
    instance = edge_modules("trigger_map").TriggerMap(
        driver, ism.ISM("OFF", UNIT, ism.LED_TABLE), None, None, mqtt)
    return instance, calls, receipts


@pytest.mark.parametrize("cache_lost", [False, True])
def test_lost_receipt_releases_only_queried_task_without_reexecution(
        bridge, tmp_path, monkeypatch, cache_lost):
    monkeypatch.setattr(physical_tasks, "PHYSICAL_TASKS_DIR", tmp_path)
    edge, calls, receipts = bridge
    state = ESP32State()
    task = "task_" + "a" * 32
    physical_tasks.reserve(task, unit_id=UNIT, session_id="session", action="SET_STATE",
                           params={"state": "ON"})
    state.mark_task_pending(task)
    state.mark_task_pending("another")
    command = {"session_id": "session", "action": "SET_STATE", "params": {"state": "ON"}}
    edge.on_mqtt(f"ssm/task/{UNIT}/{task}", command)
    assert calls == ["ON"]
    if cache_lost:
        edge._recent_results.clear()  # e.g. device restarted after execution
    query = physical_tasks.receipt_query(task)
    edge.on_mqtt(f"ssm/task_query/{UNIT}/{task}", query)
    result = receipts[-1][1]
    assert receipts[-1][2]["qos"] == 1
    assert result["result"] == ("unknown" if cache_lost else "ok")
    assert physical_tasks.record_result(task, UNIT, result)
    state.store_task_result(task, result)
    assert state.has_pending_tasks()  # other work still blocks admission
    state.store_task_result("another", {"result": "cancelled"})
    assert not state.has_pending_tasks()
    state.mark_task_pending(task)  # delayed task loopback cannot reopen it
    assert not state.has_pending_tasks()
    edge.on_mqtt(f"ssm/task/{UNIT}/{task}", command)
    assert calls == ["ON"]  # even an unknown result must not retry the action
    assert physical_tasks.get(task, unit_id=UNIT, session_id="session")["result"] == result["result"]


def test_query_does_not_end_an_active_finite_blink(bridge):
    edge, calls, receipts = bridge
    edge._pending_blink = ("blink", "session")
    edge.on_mqtt(f"ssm/task_query/{UNIT}/blink", {"task_id": "blink"})
    assert receipts[-1][1]["result"] == "executing"
    assert edge._pending_blink == ("blink", "session")
    assert not calls


def test_unknown_requires_explicit_device_release_and_keeps_late_success(tmp_path, monkeypatch):
    monkeypatch.setattr(physical_tasks, "PHYSICAL_TASKS_DIR", tmp_path)
    physical_tasks.reserve("task", unit_id=UNIT, session_id="s", action="SET_STATE", params={})
    state = ESP32State()
    state.mark_task_pending("task")
    result = {"result": "unknown", "error": "receipt_unavailable"}
    assert not physical_tasks.record_result("task", UNIT, result)
    state.store_task_result("task", result)
    assert state.has_pending_tasks()
    result["execution_active"] = False
    assert physical_tasks.record_result("task", UNIT, result)
    state.store_task_result("task", result)
    assert not state.has_pending_tasks()
    assert not physical_tasks.record_result("task", UNIT, {"result": "executing"})
    state.store_task_result("task", {"result": "executing"})
    assert state.get_task_result("task")["result"] == "unknown"
    assert physical_tasks.record_result("task", UNIT, {"result": "ok"})
    assert not physical_tasks.record_result("task", UNIT, result)
    assert physical_tasks.get("task", unit_id=UNIT, session_id="s")["result"] == "ok"


def test_receipt_queries_are_delayed_and_rate_limited(monkeypatch):
    now = [0]
    monkeypatch.setattr("cloud.esp32.state.time.monotonic", lambda: now[0])
    state = ESP32State()
    state.mark_task_pending("task")
    assert state.receipt_queries_due() == []
    now[0] = 5
    assert state.receipt_queries_due() == ["task"]
    assert state.receipt_queries_due() == []
    state.store_task_result("task", {"result": "ok"})
    now[0] = 10
    assert state.receipt_queries_due() == []


def test_qos_receipts_queue_outside_callback_and_survive_disconnect(edge_modules, monkeypatch):
    monkeypatch.setitem(sys.modules, "ujson", json)
    monkeypatch.setitem(sys.modules, "network", SimpleNamespace(
        STA_IF=0, WLAN=lambda _: SimpleNamespace(isconnected=lambda: True)))
    monkeypatch.setitem(sys.modules, "umqtt", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "umqtt.simple", SimpleNamespace(MQTTClient=object))
    module = edge_modules("mqtt_client")
    monkeypatch.setattr(module, "time", SimpleNamespace(ticks_ms=lambda: 1, ticks_diff=lambda a, b: a-b))
    transport = module.MqttClient()
    sent = []
    fail = [True]

    def publish(topic, payload, **kw):
        sent.append((topic, payload, kw))
        if fail[0]:
            raise OSError("connection lost before PUBACK")
        # Receiving another message while waiting for PUBACK only queues its receipt.
        transport.publish("second", {"result": "ok"}, qos=1)

    client = SimpleNamespace(sock=SimpleNamespace(settimeout=lambda _: None, close=lambda: None),
                             publish=publish, check_msg=lambda: None)
    transport._client = client
    transport.publish("first", {"result": "ok"}, qos=1)
    assert not sent
    transport.check_msg()
    assert transport._client is None
    assert len(transport._outbox) == 1
    fail[0] = False
    transport._client = client
    transport.check_msg()
    assert len(sent) == 2
    assert sent[-1][2]["qos"] == 1
    assert [item[0] for item in transport._outbox] == ["second"]
