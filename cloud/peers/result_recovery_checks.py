"""Late lamp observations must resolve uncertain peer requests without replay."""
import time
from types import SimpleNamespace

import pytest

from cloud.esp32 import physical_tasks
from cloud.peers import gateway
from cloud.space.execution import ExecutionContext


@pytest.fixture
def peer_request(monkeypatch, tmp_path):
    monkeypatch.setattr(physical_tasks, "PHYSICAL_TASKS_DIR", tmp_path)
    context = ExecutionContext("owner", "visitor", False, expires_at=time.time() + 60)
    entry = gateway._Request("lamp-peer_request", context=context)
    entry.result = {"request_id": "peer_review", "target_unit_id": "esp32_desk_led",
                    "result": "unknown", "error": "edge_result_unconfirmed"}
    monkeypatch.setattr(gateway, "_requests", {"peer_review": entry})
    monkeypatch.setattr(gateway, "_esp32_state", None)
    return entry


def reserve():
    physical_tasks.reserve("peer_review", unit_id="esp32_desk_led", session_id="owner",
                           action="BLINK", params={"count": 2}, origin_kind="user_delegated")


@pytest.mark.parametrize("prior", ["unknown", "accepted", "executing", "timeout"])
def test_durable_completion_resolves_each_uncertain_state_without_republish(peer_request, prior):
    peer_request.result["result"] = prior
    reserve()
    physical_tasks.record_result("peer_review", "esp32_desk_led", {
        "result": "ok", "task_id": "peer_review", "unit_id": "esp32_desk_led", "session_id": "owner"})
    assert gateway.get_request_result("peer_review")["result"] == "ok"
    assert peer_request.ready.is_set()


@pytest.mark.parametrize("result", ["ok", "blocked", "rejected", "error", "cancelled"])
def test_memory_observation_preserves_real_terminal_outcome(peer_request, monkeypatch, result):
    monkeypatch.setattr(gateway, "_esp32_state", SimpleNamespace(get_task_result=lambda _: {
        "result": result, "task_id": "peer_review", "unit_id": "esp32_desk_led", "session_id": "owner"}))
    assert gateway.get_request_result("peer_review")["result"] == result


@pytest.mark.parametrize("field,value", [("task_id", "other"), ("unit_id", "other_lamp"),
                                         ("session_id", "other_session")])
def test_unrelated_memory_receipt_cannot_complete_request(peer_request, monkeypatch, field, value):
    physical = {"result": "ok", "task_id": "peer_review", "unit_id": "esp32_desk_led", "session_id": "owner"}
    physical[field] = value
    monkeypatch.setattr(gateway, "_esp32_state", SimpleNamespace(get_task_result=lambda _: physical))
    assert gateway.get_request_result("peer_review")["result"] == "unknown"


def test_other_session_durable_record_does_not_complete_request(peer_request):
    physical_tasks.reserve("peer_review", unit_id="esp32_desk_led", session_id="other",
                           action="BLINK", params={"count": 2})
    physical_tasks.record_result("peer_review", "esp32_desk_led", {"result": "ok"})
    assert gateway.get_request_result("peer_review")["result"] == "unknown"


@pytest.mark.parametrize("pending", ["accepted", "executing", "unknown", "timeout"])
def test_pending_receipt_never_becomes_completion(peer_request, monkeypatch, pending):
    reserve()
    physical_tasks.record_result("peer_review", "esp32_desk_led", {"result": pending})
    monkeypatch.setattr(gateway, "_esp32_state", SimpleNamespace(get_task_result=lambda _: {"result": pending}))
    assert gateway.get_request_result("peer_review")["result"] == "unknown"


def test_completed_peer_result_does_not_regress(peer_request, monkeypatch):
    peer_request.result["result"] = "ok"
    monkeypatch.setattr(gateway, "_esp32_state", SimpleNamespace(get_task_result=lambda _: {"result": "error"}))
    assert gateway.get_request_result("peer_review")["result"] == "ok"
