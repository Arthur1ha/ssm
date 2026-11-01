#!/usr/bin/env python3
"""Read session event logs without contacting devices."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path


def _files(log_dir: Path) -> list[Path]:
    return sorted(log_dir.glob("session-*.jsonl"))


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
    kind = event.get("event")
    for key in ("turn", "node", "thread_id", "checkpoint_id", "tool",
                "target_unit_id", "initiator_unit_id", "skill_id",
                "request_id", "task_id", "transport", "status", "duration_ms",
                "goal", "input", "output", "decision", "params", "result",
                "reply", "response", "question", "error", "cause", "message"):
        if key in event and event[key] is not None:
            if kind == "model.request" and key == "input" and event.get("goal"):
                continue  # The prompt is available with --json; the goal is the useful live summary.
            raw = event[key]
            if key == "result" and isinstance(raw, dict) and kind == "tool.result":
                raw = {name: raw[name] for name in
                       ("result", "error", "message", "response", "task_id", "actions_taken")
                       if name in raw}
                nested = event[key].get("tool_results")
                if isinstance(nested, list):
                    raw["tool_results"] = [
                        {"tool": item.get("tool"),
                         "status": (item.get("result") or {}).get("result")}
                        for item in nested[:12] if isinstance(item, dict)]
            value = (json.dumps(raw, ensure_ascii=False, default=str) if isinstance(raw, (dict, list))
                     else str(raw))
            value = value.replace("\n", " ")
            limit = 1000 if key in {"decision", "params", "result", "reply", "response", "output"} else 320
            preview = value[:limit]
            if len(value) > limit:
                preview += "…"
            parts.append(f"{key}={preview}")
    return "  ".join(parts)


def _select(events: list[dict], args: argparse.Namespace) -> list[dict]:
    if args.errors:
        events = [event for event in events if _error(event)]
    identifier = args.identifier
    if args.latest and not identifier:
        for event in reversed(events):
            if event.get("session_id") and event["session_id"] != "system":
                identifier = event["session_id"]
                break
        if not identifier and events:
            identifier = "system"
    if identifier:
        events = [event for event in events if (event.get("session_id") or "system") == identifier
                  or event.get("trace_id") == identifier]
    if args.agent_id:
        events = [event for event in events if event.get("agent_id") == args.agent_id]
    if args.run_id:
        events = [event for event in events if event.get("run_id") == args.run_id]
    if args.task_id:
        events = [event for event in events if event.get("task_id") == args.task_id
                  or event.get("request_id") == args.task_id]
    if not args.json:
        events = [event for event in events if event.get("event") != "persona.loaded"]
    return events


def _follow(log_dir: Path, args: argparse.Namespace) -> None:
    """Tail new complete lines without retaining old events or rereading files."""
    positions: dict[Path, tuple[int, int]] = {}
    for path in _files(log_dir):
        try:
            stat = path.stat()
            positions[path] = (stat.st_ino, stat.st_size)
        except OSError:
            pass
    target = args.identifier or ("errors" if args.errors else "latest session")
    print(f"Watching new events for {target}...", flush=True)
    while True:
        paths = _files(log_dir)
        latest = None
        if args.latest and not args.identifier:
            candidates = [path for path in paths if path.name != "session-system.jsonl"]
            if candidates:
                latest = max(candidates, key=lambda path: path.stat().st_mtime_ns).stem[8:]
            elif paths:
                latest = "system"
        for path in paths:
            try:
                with path.open("rb") as source:
                    stat = path.stat()
                    previous = positions.get(path)
                    if previous is None:
                        offset = 0
                    elif previous[0] != stat.st_ino or previous[1] > stat.st_size:
                        offset = 0
                    else:
                        offset = previous[1]
                    source.seek(offset)
                    while True:
                        line = source.readline()
                        if not line:
                            break
                        if not line.endswith(b"\n"):
                            source.seek(-len(line), 1)
                            break
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        if args.errors and not _error(event):
                            continue
                        if args.identifier and not (
                            (event.get("session_id") or "system") == args.identifier
                            or event.get("trace_id") == args.identifier
                        ):
                            continue
                        if latest and (event.get("session_id") or "system") != latest:
                            continue
                        if args.agent_id and event.get("agent_id") != args.agent_id:
                            continue
                        if args.run_id and event.get("run_id") != args.run_id:
                            continue
                        if args.task_id and event.get("task_id") != args.task_id and event.get("request_id") != args.task_id:
                            continue
                        if not args.json and event.get("event") == "persona.loaded":
                            continue
                        print(json.dumps(event, ensure_ascii=False, sort_keys=True) if args.json else _brief(event), flush=True)
                    positions[path] = (stat.st_ino, source.tell())
            except OSError as exc:
                print(f"Cannot read {path}: {exc}", file=sys.stderr)
        positions = {path: state for path, state in positions.items() if path in paths}
        time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("identifier", nargs="?", help="session ID (or trace ID for one task)")
    parser.add_argument("--log-dir", type=Path, default=Path(__file__).resolve().parents[1] / "logs")
    parser.add_argument("--json", action="store_true", help="show full redacted JSON events")
    parser.add_argument("--latest", action="store_true", help="show the latest user session")
    parser.add_argument("--errors", action="store_true", help="show failures across traces")
    parser.add_argument("--agent-id", help="filter by exact agent ID")
    parser.add_argument("--run-id", help="filter by exact run ID")
    parser.add_argument("--task-id", help="filter by exact task or request ID")
    parser.add_argument("--follow", action="store_true", help="update the view every second")
    parser.add_argument("--limit", type=int, default=0, help="show at most this many recent events")
    args = parser.parse_args()
    for name in ("identifier", "agent_id", "run_id", "task_id"):
        value = getattr(args, name)
        if value and not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
            parser.error(f"{name} contains unsupported characters")
    if args.limit < 0:
        parser.error("--limit must be nonnegative")

    if args.follow:
        _follow(args.log_dir, args)
        return 0

    events, damaged = _read(args.log_dir)
    matches = _select(events, args)
    if args.limit:
        matches = matches[-args.limit :]
    for event in matches:
        print(json.dumps(event, ensure_ascii=False, sort_keys=True) if args.json else _brief(event), flush=True)
    if damaged:
        print(f"Warning: skipped {damaged} malformed JSONL line(s)", file=sys.stderr)
    if not _files(args.log_dir):
        print(f"No session log files in {args.log_dir}", file=sys.stderr)
    elif not matches:
        print(f"No matching events for {args.identifier or ('errors' if args.errors else 'latest session')}", file=sys.stderr)
    return 0 if matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
