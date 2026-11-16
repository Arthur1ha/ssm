"""Trace CLI checks using temporary logs, never running services or devices."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "trace_task.py"
CHAT = "chat_" + "a" * 40
LOGIN = "ses_" + "b" * 24


def write_log(directory, session, events):
    path = directory / f"session-{session}.jsonl"
    path.write_text("".join(json.dumps({"session_id": session, **event}) + "\n"
                            for event in events))
    return path


def run_trace(directory, *arguments):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--log-dir", str(directory), "--json", *arguments],
        capture_output=True, text=True, check=False,
    )
    return result, [json.loads(line) for line in result.stdout.splitlines()]


def test_latest_selects_page_request_and_excludes_newer_offline_fixtures(tmp_path):
    write_log(tmp_path, LOGIN, [{"ts": "2026-11-16T01:00:00Z", "event": "task.received"}])
    write_log(tmp_path, CHAT, [
        {"ts": "2026-11-16T02:00:00Z", "event": "task.received", "trace_id": "tr_real"},
        {"ts": "2026-11-16T02:00:01Z", "event": "reply.emitted", "trace_id": "tr_real"},
    ])
    for session in ("chat_fixture", "ses_fixture", "offline-check", "system"):
        write_log(tmp_path, session, [{"ts": "2026-11-16T03:00:00Z", "event": "task.received"}])
    result, events = run_trace(tmp_path, "--latest")
    assert result.returncode == 0, result.stderr
    assert len(events) == 2
    assert {event["session_id"] for event in events} == {CHAT}


def test_latest_without_user_logs_does_not_report_a_fixture_as_chat(tmp_path):
    write_log(tmp_path, "chat_fixture", [{"ts": "2026-11-16T03:00:00Z", "event": "task.received"}])
    result, events = run_trace(tmp_path, "--latest")
    assert result.returncode == 1
    assert events == []
    explicit, events = run_trace(tmp_path, "chat_fixture")
    assert explicit.returncode == 0
    assert len(events) == 1


def test_trace_id_and_login_scope_still_select_explicit_events(tmp_path):
    write_log(tmp_path, LOGIN, [{"ts": "2026-11-16T01:00:00Z", "event": "task.received", "trace_id": "tr_named"}])
    write_log(tmp_path, CHAT, [{"ts": "2026-11-16T02:00:00Z", "event": "tool.result", "trace_id": "tr_other"}])
    result, events = run_trace(tmp_path, "tr_named", "--latest")
    assert result.returncode == 0
    assert [event["session_id"] for event in events] == [LOGIN]
    result, events = run_trace(tmp_path, "--latest")
    assert result.returncode == 0
    assert [event["session_id"] for event in events] == [LOGIN]


def test_follow_uses_current_page_scope_and_ignores_fixture_updates(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("trace_task_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    login_path = write_log(tmp_path, LOGIN, [])
    chat_path = write_log(tmp_path, CHAT, [])
    fixture_path = write_log(tmp_path, "chat_fixture", [])
    original_files = module._files
    reads = 0

    def append_events(directory):
        nonlocal reads
        reads += 1
        if reads == 2:
            for index, path in enumerate((login_path, chat_path, fixture_path)):
                session = path.stem[8:]
                path.write_text(json.dumps({"session_id": session, "ts": str(index),
                                            "event": "task.received"}) + "\n")
                os.utime(path, ns=(1_000_000_000 + index, 1_000_000_000 + index))
        return original_files(directory)

    def finish(_):
        raise InterruptedError("one follow pass")

    monkeypatch.setattr(module, "_files", append_events)
    monkeypatch.setattr(module.time, "sleep", finish)
    args = module.argparse.Namespace(identifier=None, latest=True, errors=False,
                                    agent_id=None, run_id=None, task_id=None, json=True)
    with pytest.raises(InterruptedError):
        module._follow(tmp_path, args)
    lines = capsys.readouterr().out.splitlines()[1:]
    assert [json.loads(line)["session_id"] for line in lines] == [CHAT]
