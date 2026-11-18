"""Run routing survives process changes without leaking across sessions."""

import asyncio
import json
import subprocess
import sys

import pytest

from cloud.agents.common.run_index import FileRunIndex


def test_run_mapping_is_idempotent_and_session_scoped(tmp_path):
    index = FileRunIndex(tmp_path)
    first = index.put_if_absent("run-1", "go2", "ses-1", "go2:ses-1")
    second = index.put_if_absent("run-1", "go2", "ses-1", "go2:ses-1")
    assert first["created"] is True
    assert second["created"] is False
    assert index.get("run-1", "ses-1", agent_id="go2")["thread_id"] == "go2:ses-1"
    assert index.get("run-1", "ses-2") is None
    assert index.get("run-1", "ses-1", agent_id="cloud_orchestrator") is None
    with pytest.raises(ValueError, match="run_id_conflict"):
        index.put_if_absent("run-1", "go2", "ses-2", "go2:ses-2")


def test_second_process_recovers_mapping(tmp_path):
    FileRunIndex(tmp_path).put_if_absent("run-2", "esp32_desk_led", "ses-3", "lamp:ses-3")
    code = """
import json, sys
from cloud.agents.common.run_index import FileRunIndex
print(json.dumps(FileRunIndex(sys.argv[1]).get('run-2', 'ses-3', agent_id='esp32_desk_led')))
"""
    output = subprocess.check_output([sys.executable, "-c", code, str(tmp_path)], text=True)
    assert json.loads(output)["thread_id"] == "lamp:ses-3"


def test_run_mapping_cleanup_and_safe_path(tmp_path):
    index = FileRunIndex(tmp_path)
    index.put_if_absent("../../run", "go2", "ses", "go2:ses")
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert index.cleanup_expired(10, now=10**12) == 1
    assert index.get("../../run", "ses") is None


def test_completed_run_pins_its_checkpoint(tmp_path):
    index = FileRunIndex(tmp_path)
    index.put_if_absent("run-old", "go2", "ses", "go2:ses")
    assert index.mark_checkpoint("run-old", "ses", "checkpoint-old")["checkpoint_id"] == "checkpoint-old"
    assert FileRunIndex(tmp_path).get("run-old", "ses")["checkpoint_id"] == "checkpoint-old"
    assert index.mark_checkpoint("run-old", "ses", "checkpoint-old")["checkpoint_id"] == "checkpoint-old"
    with pytest.raises(ValueError, match="checkpoint_conflict"):
        index.mark_checkpoint("run-old", "ses", "checkpoint-new")


@pytest.mark.asyncio
async def test_async_session_admission_is_serial(tmp_path):
    index = FileRunIndex(tmp_path)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first():
        async with index.async_session_lock("go2", "ses"):
            first_entered.set()
            await release_first.wait()

    async def second():
        async with index.async_session_lock("go2", "ses"):
            second_entered.set()

    first_task = asyncio.create_task(first())
    await first_entered.wait()
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0.1)
    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set()
