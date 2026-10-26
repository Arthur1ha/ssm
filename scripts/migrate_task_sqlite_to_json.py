"""Offline, repeatable SQLite task-ledger migration.

Stop API and orchestrator writers and back up source databases (including WAL)
first. This script opens each existing source read-only and never deletes it.
Use --apply only after those steps; without it the script prints counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from cloud.agent_runtime.json_files import locked_directory, read_record, record_path, write_record


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def source_rows(source: Path, kind: str):
    uri = source.resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    try:
        if kind == "runs":
            for row in db.execute("SELECT run_id,session_id,goal,deadline,status,response,question,updated_at FROM runs"):
                steps = [json.loads(item[0]) for item in db.execute(
                    "SELECT data FROM steps WHERE run_id=? ORDER BY ordinal", (row[0],))]
                yield row[0], {"schema_version": 1, "run_id": row[0], "session_id": row[1],
                               "goal": row[2], "deadline": row[3], "status": row[4],
                               "response": row[5], "question": row[6], "updated_at": row[7],
                               "steps": steps, "trace_id": "", "parent_run_id": "",
                               "agent_id": "", "origin_kind": ""}
        else:
            for task_id, fingerprint, result in db.execute(
                "SELECT task_id,fingerprint,result FROM semantic_tasks"):
                parsed = json.loads(result)
                yield task_id, {"schema_version": 1, "task_id": task_id,
                                "fingerprint": json.loads(fingerprint), "result": parsed,
                                "physical_task_id": parsed.get("physical_task_id")}
    finally:
        db.close()


def migrate(source: Path, dest: Path, kind: str, apply: bool) -> tuple[int, int, int]:
    if not source.is_file():
        return 0, 0, 0
    records = list(source_rows(source, kind))
    steps = sum(len(data["steps"]) for _, data in records) if kind == "runs" else 0
    ids = [record_id for record_id, _ in records]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate IDs in {source}")
    copied = 0
    if apply:
        with locked_directory(dest):
            # Preflight all collisions before writing any target.
            for record_id, expected in records:
                existing = read_record(record_path(dest, record_id))
                if existing is not None and digest(existing) != digest(expected):
                    raise ValueError(f"target differs: {dest}/{record_id}.json")
            for record_id, expected in records:
                target = record_path(dest, record_id)
                if read_record(target) is None:
                    if not write_record(target, expected, exclusive=True):
                        raise RuntimeError(f"concurrent target writer: {target}")
                    copied += 1
            for record_id, expected in records:
                actual = read_record(record_path(dest, record_id))
                if digest(actual) != digest(expected):
                    raise RuntimeError(f"verification failed: {record_id}")
    return len(records), steps, copied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--kind", choices=("runs", "semantic_tasks"), required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.source.exists():
        print(f"source absent; skipped: {args.source}")
        return 0
    count, steps, copied = migrate(args.source, args.dest, args.kind, args.apply)
    print(f"{args.source}: {count} records, {steps} steps; {copied} written; target={args.dest}")
    if not args.apply:
        print("Dry run. Stop writers, back up SQLite and WAL files, then add --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
