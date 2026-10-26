import json
import multiprocessing
import subprocess
import sys
from pathlib import Path
import pytest

from cloud.observability import bind_context, configure_logging, emit_agent_event


def _writer(directory: str, service: str, count: int) -> None:
    configure_logging(service, log_dir=Path(directory))
    with bind_context(session_id="ses_shared", trace_id="tr_parallel", agent_id=service):
        for turn in range(count):
            emit_agent_event("model.decision", turn=turn, decision={"tool": "none"})


def test_event_redacts_nested_secrets_and_restores_context(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "supersecret123")
    configure_logging("api", log_dir=tmp_path)
    configure_logging("api", log_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []
    with bind_context(trace_id="tr_one"):
        emit_agent_event("model.request", input={"api_key": "plain", "prompt": "Bearer supersecret123"})
    emit_agent_event("run.finished", status="ok")
    rows = [json.loads(line) for line in next(tmp_path.glob("*.jsonl")).read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["trace_id"] == "tr_one"
    assert "trace_id" not in rows[1]
    assert rows[0]["input"]["api_key"] == "[REDACTED]"
    assert "supersecret123" not in json.dumps(rows)
    assert oct(next(tmp_path.glob("*.jsonl")).stat().st_mode & 0o777) == "0o600"


def test_processes_append_complete_events_to_one_session_file(tmp_path):
    processes = [multiprocessing.Process(target=_writer, args=(str(tmp_path), service, 40))
                 for service in ("api", "orchestrator")]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    paths = list(tmp_path.glob("session-*.jsonl"))
    assert [path.name for path in paths] == ["session-ses_shared.jsonl"]
    rows = [json.loads(line) for line in paths[0].read_text().splitlines()]
    assert len(rows) == 80
    assert {row["service"] for row in rows} == {"api", "orchestrator"}


def test_deleted_session_file_is_recreated_on_next_event(tmp_path):
    configure_logging("api", log_dir=tmp_path)
    with bind_context(session_id="ses_again"):
        emit_agent_event("run.started", goal="first")
        path = tmp_path / "session-ses_again.jsonl"
        path.unlink()
        emit_agent_event("run.finished", status="ok")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["run.finished"]


def test_session_query_shows_model_io_and_tool_result(tmp_path):
    configure_logging("api", log_dir=tmp_path)
    with bind_context(session_id="ses_visible", agent_id="go2"):
        emit_agent_event("model.request", input="站起来")
        emit_agent_event("model.response", output='{"tool":"go2_sport"}')
        emit_agent_event("tool.dispatch", tool="go2_sport", target_unit_id="go2",
                         skill_id="stand_up", params={"cmd": "StandUp"})
        emit_agent_event("tool.result", tool="go2_sport", status="ok", result={"result": "ok"})
    script = Path(__file__).resolve().parents[3] / "scripts" / "trace_task.py"
    result = subprocess.run([sys.executable, str(script), "ses_visible", "--log-dir", str(tmp_path)],
                            capture_output=True, text=True, check=True)
    assert "input=站起来" in result.stdout
    assert 'output={"tool":"go2_sport"}' in result.stdout
    assert 'params={"cmd": "StandUp"}' in result.stdout
    assert "target_unit_id=go2" in result.stdout
    assert "skill_id=stand_up" in result.stdout
    assert 'result={"result": "ok"}' in result.stdout


def test_large_input_is_bounded_and_unknown_events_are_rejected(tmp_path):
    configure_logging("api", log_dir=tmp_path)
    emit_agent_event("model.request", input={"messages": ["x" * 1000] * 20})
    row = json.loads(next(tmp_path.glob("*.jsonl")).read_text().splitlines()[-1])
    assert row["input"]["truncated"] is True
    assert row["input"]["length"] > 8192
    with pytest.raises(ValueError):
        emit_agent_event("arbitrary.event")
