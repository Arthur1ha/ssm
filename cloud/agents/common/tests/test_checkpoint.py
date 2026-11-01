"""File checkpoint contract and recovery tests."""

import asyncio
import json
import os
import subprocess
import sys

import pytest
from langgraph.checkpoint.conformance import checkpointer_test, validate
from langgraph.checkpoint.conformance.test_utils import (
    generate_checkpoint,
    generate_config,
    generate_metadata,
)
from langgraph.graph import START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from cloud.agents.common.checkpoint import FileCheckpointSaver


@checkpointer_test(name="FileCheckpointSaver")
async def file_backend():
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as root:
        yield FileCheckpointSaver(root)


@pytest.mark.asyncio
async def test_official_base_conformance():
    report = await validate(file_backend)
    assert report.passed_all_base(), report.results


def test_put_get_list_and_pending_writes(tmp_path):
    saver = FileCheckpointSaver(tmp_path)
    config = generate_config("thread/../../safe")
    checkpoint = generate_checkpoint(channel_values={"answer": "yes"})
    checkpoint["channel_versions"] = {"answer": 1}
    stored = saver.put(config, checkpoint, generate_metadata(), {"answer": 1})
    saver.put_writes(stored, [("answer", "observed")], "task-1")

    reopened = FileCheckpointSaver(tmp_path)
    loaded = reopened.get_tuple(stored)
    assert loaded.checkpoint["channel_values"] == {"answer": "yes"}
    assert loaded.pending_writes == [("task-1", "answer", "observed")]
    assert [item.checkpoint["id"] for item in reopened.list(config)] == [checkpoint["id"]]
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_concurrent_processes_do_not_lose_pending_writes(tmp_path):
    saver = FileCheckpointSaver(tmp_path)
    config = generate_config("concurrent-thread")
    checkpoint = generate_checkpoint()
    stored = saver.put(config, checkpoint, generate_metadata(), {})
    code = """
import json, sys
from cloud.agents.common.checkpoint import FileCheckpointSaver
root, config, task = sys.argv[1:]
FileCheckpointSaver(root).put_writes(json.loads(config), [('observed', task)], task)
"""
    args = [sys.executable, "-c", code, str(tmp_path), json.dumps(stored)]
    processes = [subprocess.Popen([*args, f"task-{index}"]) for index in range(8)]
    assert all(process.wait(timeout=15) == 0 for process in processes)
    loaded = FileCheckpointSaver(tmp_path).get_tuple(stored)
    assert {write[0] for write in loaded.pending_writes} == {f"task-{index}" for index in range(8)}


class _State(TypedDict):
    answer: str


def _asking_graph(saver):
    builder = StateGraph(_State)

    def ask(_state):
        return {"answer": interrupt("answer?")}

    builder.add_node("ask", ask)
    builder.add_edge(START, "ask")
    return builder.compile(checkpointer=saver)


def test_interrupt_resumes_in_new_process(tmp_path):
    config = {"configurable": {"thread_id": "resume-thread"}}
    graph = _asking_graph(FileCheckpointSaver(tmp_path))
    interrupted = graph.invoke({"answer": ""}, config, durability="sync")
    assert "__interrupt__" in interrupted
    code = """
import sys
from langgraph.graph import START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict
from cloud.agents.common.checkpoint import FileCheckpointSaver
class State(TypedDict):
    answer: str
def ask(state):
    return {'answer': interrupt('answer?')}
builder = StateGraph(State)
builder.add_node('ask', ask)
builder.add_edge(START, 'ask')
graph = builder.compile(checkpointer=FileCheckpointSaver(sys.argv[1]))
result = graph.invoke(Command(resume='yes'), {'configurable': {'thread_id': 'resume-thread'}}, durability='sync')
assert result['answer'] == 'yes', result
"""
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True, timeout=15)
    assert graph.get_state(config).values["answer"] == "yes"


def test_corrupt_checkpoint_file_is_reported(tmp_path):
    saver = FileCheckpointSaver(tmp_path)
    config = {"configurable": {"thread_id": "corrupt"}}
    path, _lock = saver._paths("corrupt")
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        saver.get_tuple(config)


def test_cleanup_skips_active_cross_process_run(tmp_path):
    saver = FileCheckpointSaver(tmp_path)
    old_config = generate_config("active-thread")
    old_stored = saver.put(old_config, generate_checkpoint(), generate_metadata(), {})
    old_path, _ = saver._paths("active-thread")
    os.utime(old_path, (1, 1))

    recent_config = generate_config("recent-thread")
    recent_stored = saver.put(recent_config, generate_checkpoint(), generate_metadata(), {})
    holder_code = """
import sys
from cloud.agents.common.checkpoint import FileCheckpointSaver
with FileCheckpointSaver(sys.argv[1]).run_lock('active-thread'):
    print('locked', flush=True)
    sys.stdin.readline()
"""
    holder = subprocess.Popen(
        [sys.executable, "-u", "-c", holder_code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert saver.cleanup_expired(60, now=120) == 0
        assert saver.get_tuple(old_stored) is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.wait(timeout=10) == 0
        assert saver.cleanup_expired(60, now=120) == 1
        assert saver.get_tuple(old_stored) is None
        assert saver.get_tuple(recent_stored) is not None
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


@pytest.mark.asyncio
async def test_async_run_lock_serializes_same_thread_without_blocking_loop(tmp_path):
    saver = FileCheckpointSaver(tmp_path)
    entered = asyncio.Event()

    async def contender():
        async with saver.async_run_lock("one-thread"):
            entered.set()

    async with saver.async_run_lock("one-thread"):
        task = asyncio.create_task(contender())
        await asyncio.sleep(0.12)
        assert not entered.is_set()
    await asyncio.wait_for(task, timeout=2)
    assert entered.is_set()
