#!/usr/bin/env python3
"""Read session event logs without contacting devices."""

from __future__ import annotations

import argparse
import hashlib
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
    for key in ("turn", "tool", "target_unit_id", "initiator_unit_id", "skill_id",
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
        return [event for event in events if _error(event)]
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
    if not args.json:
        events = [event for event in events if event.get("event") != "persona.loaded"]
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("identifier", nargs="?", help="session ID (or trace ID for one task)")
    parser.add_argument("--log-dir", type=Path, default=Path(__file__).resolve().parents[1] / "logs")
    parser.add_argument("--json", action="store_true", help="show full redacted JSON events")
    parser.add_argument("--latest", action="store_true", help="show the latest user session")
    parser.add_argument("--errors", action="store_true", help="show failures across traces")
    parser.add_argument("--follow", action="store_true", help="update the view every second")
    parser.add_argument("--limit", type=int, default=0, help="show at most this many recent events")
    args = parser.parse_args()
    if args.errors and args.identifier:
        parser.error("--errors cannot be combined with an ID")
    if args.identifier and not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", args.identifier):
        parser.error("ID contains unsupported characters")
    if args.limit < 0:
        parser.error("--limit must be nonnegative")

    seen: set[str] = set()
    had_match = False
    reported_damage = 0
    if args.follow:
        target = args.identifier or ("errors" if args.errors else "latest session")
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
                print(f"No session log files in {args.log_dir}", file=sys.stderr)
            elif not had_match:
                print(f"No matching events for {args.identifier or ('errors' if args.errors else 'latest session')}", file=sys.stderr)
            return 0 if had_match else 1
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
