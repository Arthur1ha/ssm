"""API-owned configuration, partitioned by space/device. Never reads old teachings.

Lock ordering for callers: device execution lock, then partition_lock. A future
executor must re-read/validate and reserve its existing ledger entry inside this
lock; release it before any network/device transport. Revocation and reservation
are thereby ordered, without holding filesystem locks over transport.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

from pydantic import TypeAdapter
from cloud.agents.common.training.models import Identifier, Partition, Record

_IDENTIFIER = TypeAdapter(Identifier)
_ROOT = Path(__file__).resolve().parents[2] / 'data' / 'training'


class StorageError(ValueError):
    pass


def _path(unit_id, space_id):
    _IDENTIFIER.validate_python(unit_id)
    _IDENTIFIER.validate_python(space_id)
    return Path(os.environ.get('SSM_TRAINING_DIR', str(_ROOT))) / space_id / f'{unit_id}.json'


def _read(path, unit_id, space_id):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return Partition(space_id=space_id, unit_id=unit_id).model_dump()
    except (OSError, ValueError) as exc:
        raise StorageError('training_storage_unreadable') from exc
    try:
        result = Partition.model_validate(data).model_dump()
        if result['unit_id'] != unit_id or result['space_id'] != space_id:
            raise ValueError('wrong partition')
        return result
    except ValueError as exc:
        raise StorageError('training_storage_invalid') from exc


def _write(path, data):
    validated = Partition.model_validate(data).model_dump()
    descriptor, name = tempfile.mkstemp(prefix=f'.{path.stem}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(validated, handle, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def partition_lock(unit_id: str, *, space_id: str, write: bool = False):
    """Shared coordination point. Writes flush only after a successful context.

    Do not nest this context for the same partition. Readers receive an isolated
    snapshot; mutations have no effect unless API code explicitly passes write.
    """
    path = _path(unit_id, space_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path.with_suffix('.lock'), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        data = _read(path, unit_id, space_id)
        before = deepcopy(data) if write else None
        yield data
        if write and data != before:
            _write(path, data)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def list_records(unit_id: str, *, space_id: str) -> list[dict]:
    with partition_lock(unit_id, space_id=space_id) as data:
        return deepcopy(list(data['records'].values()))


def commit_locked(data: dict, record: dict, *, expected_revision: int, proposal_id: str) -> dict:
    """For API transactions already holding partition_lock(write=True)."""
    _IDENTIFIER.validate_python(proposal_id)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('invalid expected_revision')
    validated = Record.model_validate(record).model_dump()
    if (validated['unit_id'], validated['space_id']) != (data['unit_id'], data['space_id']):
        raise ValueError('partition_identity_mismatch')
    previous = data['confirmed_proposals'].get(proposal_id)
    if previous is not None:
        if previous['teaching_id'] != validated['teaching_id']:
            raise ValueError('proposal_identity_mismatch')
        return deepcopy(previous)
    current = data['records'].get(validated['teaching_id'])
    if (current is None and expected_revision != 0) or (current is not None and current['revision'] != expected_revision):
        raise ValueError('revision_conflict')
    if current is None and len(data['records']) >= 500:
        raise ValueError('device_record_limit')
    if current is not None and len(current['history']) >= 100:
        raise ValueError('history_limit')
    history = deepcopy(current['history']) if current else []
    if current:
        history.append({key: deepcopy(value) for key, value in current.items() if key != 'history'})
    now = time.time()
    updated = validated | {'revision': expected_revision + 1, 'loaded_revision': None,
                           'history': history, 'created_at': current['created_at'] if current else now,
                           'updated_at': now}
    updated = Record.model_validate(updated).model_dump()
    data['records'][updated['teaching_id']] = updated
    data['loaded_epoch'].pop(updated['teaching_id'], None)
    data['confirmed_proposals'][proposal_id] = deepcopy(updated)
    data['cursor'] += 1
    return deepcopy(updated)


def commit(record: dict, *, expected_revision: int, proposal_id: str) -> dict:
    validated = Record.model_validate(record).model_dump()
    with partition_lock(validated['unit_id'], space_id=validated['space_id'], write=True) as data:
        return commit_locked(data, validated, expected_revision=expected_revision, proposal_id=proposal_id)
