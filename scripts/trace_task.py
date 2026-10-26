#!/usr/bin/env python3
"""Read the rotated, per-process agent event logs without contacting devices."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import sys
import time
from pathlib import Path


def _files(log_dir: Path) -> list[Path]:
    return sorted(Path(name) for name in glob.glob(str(log_dir / "agent-trace-*.jsonl*")))


def _read(log_dir: Path) -> tuple[list[dict], int]:
    events: list[dict] = []
    damaged = 0
    for path in _files(log_dir):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as source:
                for line in source:
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict) and isinstance(event.get("ts"), str):
                            events.append(event)
                        else:
                            damaged += 1
                    except json.JSONDecodeError:
                        damaged += 1
        except OSError as exc:
            print(f"Cannot read {path}: {exc}", file=sys.stderr)
    events.sort(key=lambda item: (item["ts"], str(item.get("service", "")), str(item.get("event", ""))))
    return events, damaged


def _error(event: dict) -> bool:
    if str(event.get("level", "")).lower() in {"error", "critical"}:
        return True
    if event.get("event") in {"model.error", "reply.fallback"}:
        return True
    if event.get("event") in {"tool.result", "peer.result", "device.result"}:
        status = event.get("status", event.get("result"))
        return isinstance(status, str) and status in {"error", "failed", "blocked", "unknown", "timeout", "rejected"}
    return False


def _brief(event: dict) -> str:
    parts = [str(event.get("ts", "?")), str(event.get("agent_id") or event.get("service") or "?"), str(event.get("event", "?"))]
    for key in ("trace_id", "turn", "tool", "status", "error", "duration_ms"):
        if key in event and event[key] is not None:
            value = str(event[key]).replace("\n", " ")
            parts.append(f"{key}={value[:160]}")
    return "  ".join(parts)


def _select(events: list[dict], args: argparse.Namespace) -> list[dict]:
    if args.errors:
        return [event for event in events if _error(event)]
    trace_id = args.trace_id
    if args.latest and not trace_id:
        for event in reversed(events):
            if event.get("trace_id"):
                trace_id = event["trace_id"]
                break
    if trace_id:
        return [event for event in events if event.get("trace_id") == trace_id]
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_id", nargs="?", help="trusted trace ID returned by the API")
    parser.add_argument("--log-dir", type=Path, default=Path(__file__).resolve().parents[1] / "logs")
    parser.add_argument("--json", action="store_true", help="show full redacted JSON events")
    parser.add_argument("--latest", action="store_true", help="show the latest trace")
    parser.add_argument("--errors", action="store_true", help="show failures across traces")
    parser.add_argument("--follow", action="store_true", help="update the view every second")
    parser.add_argument("--limit", type=int, default=0, help="show at most this many recent events")
    args = parser.parse_args()
    if args.errors and args.trace_id:
        parser.error("--errors cannot be combined with a trace ID")
    if args.trace_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", args.trace_id):
        parser.error("trace ID contains unsupported characters")
    if args.limit < 0:
        parser.error("--limit must be nonnegative")

    seen: set[str] = set()
    had_match = False
    reported_damage = 0
    if args.follow:
        target = args.trace_id or ("errors" if args.errors else "latest task")
        print(f"Watching {target}; waiting for new task events...", flush=True)
    while True:
        events, damaged = _read(args.log_dir)
        matches = _select(events, args)
        if args.limit:
            matches = matches[-args.limit :]
        new = []
        for event in matches:
            key = hashlib.sha256(json.dumps(event, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if key not in seen:
                seen.add(key)
                new.append(event)
        for event in new:
            print(json.dumps(event, ensure_ascii=False, sort_keys=True) if args.json else _brief(event), flush=True)
        had_match = had_match or bool(matches)
        if damaged and damaged != reported_damage:
            print(f"Warning: skipped {damaged} malformed JSONL line(s)", file=sys.stderr)
            reported_damage = damaged
        if not args.follow:
            if not _files(args.log_dir):
                print(f"No agent trace log files in {args.log_dir}", file=sys.stderr)
            elif not had_match:
                print(f"No matching events for {args.trace_id or ('errors' if args.errors else 'latest task')}", file=sys.stderr)
            return 0 if had_match else 1
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
