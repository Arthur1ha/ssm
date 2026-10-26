"""Ledger safety across processes and damaged records."""

from concurrent.futures import ProcessPoolExecutor

import pytest

from cloud.agent_runtime.json_files import CorruptRecordError
from cloud.agent_runtime.loop import Run
from cloud.agent_runtime.store import RunStore


def _reserve(path: str):
    return RunStore(path).create(Run("same_id", "session", "goal", 1000))


def _claim(path: str):
    return RunStore(path).claim_resume("same_id", "session") is not None


def _reserve_semantic(path: str):
    from pathlib import Path
    from cloud.esp32 import tasks
    tasks.TASKS_DIR = Path(path)
    return tasks._reserve("semantic_same_id", ("session", "lamp", "goal"))


def test_run_id_and_resume_claim_are_process_atomic(tmp_path):
    path = str(tmp_path / "runs")
    with ProcessPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(_reserve, [path] * 8)) == 1
    store = RunStore(path)
    run = store.get("same_id", "session")
    run.status = "needs_clarification"
    run.question = "which?"
    store.save(run)
    with ProcessPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(_claim, [path] * 8)) == 1


def test_corrupt_record_cannot_be_recreated_or_overwritten(tmp_path):
    store = RunStore(tmp_path / "runs")
    assert store.create(Run("damaged", "session", "goal", 1000))
    file = store.path / "damaged.json"
    file.write_text("broken", encoding="utf-8")
    with pytest.raises(CorruptRecordError):
        store.get("damaged", "session")
    assert (store.path / "damaged.json.corrupt").exists()
    assert not store.create(Run("damaged", "session", "goal", 1000))
    with pytest.raises(CorruptRecordError):
        store.save(Run("damaged", "session", "goal", 1000))


def test_record_id_rejects_path_traversal(tmp_path):
    store = RunStore(tmp_path / "runs")
    with pytest.raises(ValueError, match="invalid_record_id"):
        store.create(Run("../escape", "session", "goal", 1000))


def test_semantic_reservation_is_process_atomic(tmp_path):
    path = str(tmp_path / "semantic_tasks")
    with ProcessPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(_reserve_semantic, [path] * 8)) == 1
