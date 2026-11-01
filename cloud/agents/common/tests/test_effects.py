"""Crash and concurrency behavior of the physical effect ledger."""

import subprocess
import sys

import pytest

from cloud.agents.common.effects import FileEffectLedger


IDENTITY = ("lamp", "lamp:session", "run-1", "step-1")


def test_reserve_reopen_unknown_and_observe(tmp_path):
    ledger = FileEffectLedger(tmp_path)
    first = ledger.reserve(*IDENTITY, "set_light", {"brightness": 2, "rgb": [1, 2, 3]})
    assert first["reservation"] == "new" and first["status"] == "reserved"
    reopened = FileEffectLedger(tmp_path)
    assert reopened.read(*IDENTITY)["status"] == "unknown"
    repeat = reopened.reserve(*IDENTITY, "set_light", {"rgb": [1, 2, 3], "brightness": 2})
    assert repeat["reservation"] == "existing" and repeat["status"] == "unknown"
    with pytest.raises(ValueError, match="conflict"):
        reopened.reserve(*IDENTITY, "set_light", {"brightness": 3})
    result = {"result": "ok", "task_id": "device-task-1"}
    assert reopened.mark_result(*IDENTITY, result)["status"] == "observed"
    assert FileEffectLedger(tmp_path).read(*IDENTITY)["result"] == result
    assert ledger.reserve(*IDENTITY, "set_light", {"brightness": 2, "rgb": [1, 2, 3]})["reservation"] == "existing"
    with pytest.raises(ValueError, match="conflict"):
        ledger.mark_result(*IDENTITY, {"result": "timeout"})


def test_unreserved_result_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="not_reserved"):
        FileEffectLedger(tmp_path).mark_result(*IDENTITY, {"result": "ok"})


def test_concurrent_processes_get_only_one_new_reservation(tmp_path):
    code = """
import sys
from cloud.agents.common.effects import FileEffectLedger
ledger = FileEffectLedger(sys.argv[1])
print(ledger.reserve('go2', 'thread', 'run', 'step', 'move', {'duration': 1})['reservation'])
"""
    processes = [subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True
    ) for _ in range(8)]
    output = [process.communicate(timeout=15)[0].strip() for process in processes]
    assert all(process.returncode == 0 for process in processes)
    assert output.count("new") == 1
    assert output.count("existing") == 7
    assert FileEffectLedger(tmp_path).read("go2", "thread", "run", "step")["status"] == "unknown"
