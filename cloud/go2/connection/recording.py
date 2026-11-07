"""Bounded read-only sensor recording for navigation replay and site acceptance."""
from __future__ import annotations

import asyncio
from cloud.agents.common.runner import invoke_blocking
import json
import time
import uuid
from pathlib import Path

import numpy as np

from cloud.observability.logging_config import LOG_DIR

MAX_RECORD_BYTES = 128 * 1024 * 1024
WRITER_SHUTDOWN_TIMEOUT_S = 5.0


async def record_navigation(robot, duration_s: float, *, root: Path | None = None) -> dict:
    """Sample received sequences without subscribing twice or issuing commands.

    Sequence gaps and queue drops are reported. A sampled recording with gaps
    must not be presented as a complete raw packet capture.
    """
    if not 0 < duration_s <= 60:
        raise ValueError('invalid_recording_duration')
    session = robot._odom_session_id
    directory = (root or LOG_DIR / 'go2-search') / ('recording_' + uuid.uuid4().hex)
    directory.mkdir(parents=True, mode=0o700)
    queue = asyncio.Queue(maxsize=8)
    summary = {'directory': str(directory), 'map_session': session, 'motion_sent': False,
               'queue_drops': 0, 'sequence_gaps': {'odom': 0, 'voxel': 0, 'video': 0},
               'records': 0, 'bytes': 0, 'reason': 'duration_complete',
               'captured_counts': {'odom': 0, 'voxel': 0, 'video': 0, 'state': 0},
               'started_at': time.time(), 'sample_period_s': .01,
               'command_evidence': 'runtime action snapshots; full dispatch/receipt logs remain in session JSONL'}
    last = {}

    def write(item):
        kind, sequence, payload, blob = item
        record = {'kind': kind, 'sequence': sequence, **payload}
        if kind == 'video':
            path = directory / f'video_{sequence}.jpg'
            path.write_bytes(blob)
            path.chmod(0o600)
            record['artifact'] = path.name
        elif kind == 'voxel':
            path = directory / f'voxel_{sequence}.npz'
            points, jpeg = blob
            with path.open('wb') as stream:
                np.savez_compressed(stream, points_odom=points,
                                    metadata=json.dumps(payload, ensure_ascii=False))
            path.chmod(0o600)
            if jpeg:
                path.with_suffix('.jpg').write_bytes(jpeg)
                path.with_suffix('.jpg').chmod(0o600)
            record['artifact'] = path.name
        else:
            path = None
        encoded = json.dumps(record, ensure_ascii=False, default=str) + '\n'
        index = directory / 'index.jsonl'
        with index.open('a') as stream:
            stream.write(encoded)
        index.chmod(0o600)
        return len(encoded.encode()) + (path.stat().st_size if path else 0) + (
            len(blob[1]) if kind == 'voxel' and blob[1] else 0)

    def write_batch(items):
        completed = []
        for item in items:
            try:
                completed.append((item[0], write(item)))
            except Exception as exc:
                # Preserve accounting for records committed before an I/O error.
                return completed, exc
        return completed, None

    async def writer():
        while True:
            item = await queue.get()
            if item is None:
                return
            batch = [item]
            finished = False
            # Amortize the blocking adapter's scheduling cost across at most
            # eight records; neither buffering nor worker count grows with input.
            while len(batch) < queue.maxsize and not queue.empty():
                item = queue.get_nowait()
                if item is None:
                    finished = True
                    break
                batch.append(item)
            completed, failure = await invoke_blocking(write_batch, batch)
            for kind, size in completed:
                summary['bytes'] += size
                summary['records'] += 1
                summary['captured_counts'][kind] += 1
            if failure is not None:
                raise failure
            if finished:
                return

    def enqueue(kind, seq, payload, blob=None):
        if seq is None or last.get(kind) == seq:
            return
        if kind in summary['sequence_gaps'] and kind in last:
            summary['sequence_gaps'][kind] += max(0, seq - last[kind] - 1)
        last[kind] = seq
        try:
            queue.put_nowait((kind, seq, payload, blob))
        except asyncio.QueueFull:
            summary['queue_drops'] += 1

    worker = asyncio.create_task(writer())
    deadline = time.monotonic() + duration_s
    failure = None
    try:
        while time.monotonic() < deadline:
            if worker.done():
                worker.result()
            if not robot.is_connected or robot._odom_session_id != session:
                summary['reason'] = 'connection_or_session_changed'
                break
            if summary['bytes'] >= MAX_RECORD_BYTES:
                summary['reason'] = 'recording_byte_limit'
                break
            now = time.monotonic()
            enqueue('odom', robot._odom_count, {'received_at': robot.odom_received_at, 'pose': robot.odom})
            captured = robot.latest_frame_snapshot()
            if captured:
                jpeg, frame = captured
                enqueue('video', frame['frame_id'], {'frame': frame}, jpeg)
                raw = robot.voxel_raw
                if raw is not None:
                    data = raw['data']
                    metadata = {**frame, 'image_received_at': frame['received_at'],
                        'voxel_received_at': robot.voxel_received_at,
                        'voxel_skew_s': robot.voxel_received_at - frame['received_at'],
                        'voxel_frame_id': robot.voxel_frame_id,
                        'voxel_resolution_m': data['resolution'], 'voxel_origin': data['origin'],
                        'voxel_width': data['width']}
                    enqueue('voxel', robot._voxel_count, metadata,
                            (data['data']['points'], jpeg))
            state = robot.state_snapshot()
            enqueue('state', state.get('revision'), {'received_at': now, 'state': state})
            await asyncio.sleep(.01)
    except asyncio.CancelledError as exc:
        summary['reason'] = 'cancelled'
        failure = exc
    except Exception as exc:
        summary['reason'] = ('writer_error' if worker.done() and not worker.cancelled()
                             and worker.exception() else 'recording_error')
        failure = exc
    finally:
        try:
            if failure is None:
                # Bound both sentinel insertion and draining: a failed/stalled
                # writer may otherwise leave a full queue waiting forever.
                async with asyncio.timeout(WRITER_SHUTDOWN_TIMEOUT_S):
                    if not worker.done():
                        await queue.put(None)
                    await worker
        except asyncio.CancelledError as exc:
            summary['reason'] = 'cancelled'
            failure = exc
        except Exception as exc:
            summary['reason'] = 'writer_timeout' if isinstance(exc, TimeoutError) else 'writer_error'
            failure = exc
        finally:
            if not worker.done():
                worker.cancel()
            # Retrieve failures even when the sampling loop already failed.
            await asyncio.gather(worker, return_exceptions=True)
        summary['ended_at'] = time.time()
        summary['complete_received_sequences'] = (summary['reason'] == 'duration_complete'
            and not summary['queue_drops'] and not any(summary['sequence_gaps'].values())
            and all(summary['captured_counts'][kind] for kind in ('odom', 'voxel', 'video')))
        output = directory / 'summary.json'
        output.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        output.chmod(0o600)
    if failure is not None:
        raise failure
    return summary
