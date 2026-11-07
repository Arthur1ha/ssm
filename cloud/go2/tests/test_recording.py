import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud.go2.connection.recording import record_navigation
from cloud.go2.connection import recording


def quiet_robot():
    return SimpleNamespace(_odom_session_id='session', is_connected=True,
        _odom_count=1, odom_received_at=1., odom={'x': 0., 'y': 0., 'heading': 0.},
        latest_frame_snapshot=lambda: None, state_snapshot=lambda: {'revision': 1})


def saved_summary(root):
    return json.loads(next(root.glob('recording_*/summary.json')).read_text())


@pytest.mark.asyncio
async def test_recording_saves_odom_without_motion_or_model(tmp_path):
    robot = SimpleNamespace(_odom_session_id='session', is_connected=True,
        _odom_count=1, odom_received_at=1., odom={'x': 0., 'y': 0., 'heading': 0.},
        latest_frame_snapshot=lambda: None,
        state_snapshot=lambda: {'revision': 1, 'last_action': {'result': 'accepted'}})
    result = await record_navigation(robot, .02, root=tmp_path)
    assert result['motion_sent'] is False
    assert result['records'] == 2
    records = [json.loads(line) for line in (Path(result['directory'])/'index.jsonl').read_text().splitlines()]
    assert {r['kind'] for r in records} == {'odom','state'}
    assert records[1]['state']['last_action']['result'] == 'accepted'


@pytest.mark.asyncio
async def test_cancelled_recording_reports_incomplete_and_propagates_cancel(tmp_path, monkeypatch):
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def stalled_write(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(recording, 'invoke_blocking', stalled_write)
    task = asyncio.create_task(record_navigation(quiet_robot(), 30, root=tmp_path))
    await asyncio.wait_for(entered.wait(), .5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, .5)
    assert stopped.is_set()
    summary = saved_summary(tmp_path)
    assert summary['reason'] == 'cancelled'
    assert not summary['complete_received_sequences']


@pytest.mark.asyncio
async def test_writer_failure_is_reported_and_propagated(tmp_path, monkeypatch):
    async def failing_write(*args):
        raise OSError('disk full')

    monkeypatch.setattr(recording, 'invoke_blocking', failing_write)
    with pytest.raises(OSError, match='disk full'):
        await asyncio.wait_for(record_navigation(quiet_robot(), .05, root=tmp_path), .5)
    summary = saved_summary(tmp_path)
    assert summary['reason'] == 'writer_error'
    assert not summary['complete_received_sequences']


@pytest.mark.asyncio
async def test_full_queue_with_stalled_writer_has_bounded_shutdown(tmp_path, monkeypatch):
    stopped = asyncio.Event()

    async def stalled_write(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    robot = quiet_robot()

    def next_state():
        robot._odom_count += 1
        return {'revision': robot._odom_count}

    robot.state_snapshot = next_state
    monkeypatch.setattr(recording, 'invoke_blocking', stalled_write)
    monkeypatch.setattr(recording, 'WRITER_SHUTDOWN_TIMEOUT_S', .02)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(record_navigation(robot, .08, root=tmp_path), .5)
    assert stopped.is_set()
    summary = saved_summary(tmp_path)
    assert summary['queue_drops'] > 0
    assert summary['reason'] == 'writer_timeout'
    assert not summary['complete_received_sequences']


@pytest.mark.asyncio
async def test_writer_keeps_up_with_fifty_records_per_second(tmp_path):
    robot = quiet_robot()
    samples = 0

    def advancing_state():
        nonlocal samples
        samples += 1
        # The recorder samples every 10 ms. Two changed sequences every four
        # samples model about 50 records/s, above one adapter call per record.
        if samples % 4 == 0:
            robot._odom_count += 1
        return {'revision': robot._odom_count}

    robot.state_snapshot = advancing_state
    result = await record_navigation(robot, .65, root=tmp_path)
    assert result['queue_drops'] == 0
    assert result['records'] >= 24
    records = [json.loads(line) for line in
               (Path(result['directory']) / 'index.jsonl').read_text().splitlines()]
    assert result['records'] == len(records)
    assert sum(result['captured_counts'].values()) == len(records)
    for kind in ('odom', 'state'):
        sequences = [record['sequence'] for record in records if record['kind'] == kind]
        assert sequences == list(range(1, sequences[-1] + 1))


@pytest.mark.asyncio
async def test_partial_batch_failure_preserves_completed_record_count(tmp_path, monkeypatch):
    original_open = Path.open
    appends = 0

    def fail_second_append(path, mode='r', *args, **kwargs):
        nonlocal appends
        if path.name == 'index.jsonl' and mode == 'a':
            appends += 1
            if appends == 2:
                raise OSError('disk full during batch')
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', fail_second_append)
    with pytest.raises(OSError, match='disk full during batch'):
        await record_navigation(quiet_robot(), .1, root=tmp_path)
    summary = saved_summary(tmp_path)
    assert summary['reason'] == 'writer_error'
    assert summary['records'] == 1
    assert summary['captured_counts']['odom'] == 1
    assert summary['captured_counts']['state'] == 0
    assert summary['bytes'] > 0
