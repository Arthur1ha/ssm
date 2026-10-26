import json
import multiprocessing
from pathlib import Path
import pytest

from cloud.observability import bind_context, configure_logging, emit_agent_event


def _writer(directory: str, service: str, count: int) -> None:
    configure_logging(service, log_dir=Path(directory))
    with bind_context(trace_id="tr_parallel", agent_id=service):
        for turn in range(count):
            emit_agent_event("model.decision", turn=turn, decision={"tool": "none"})


def test_event_redacts_nested_secrets_and_restores_context(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "supersecret123")
    configure_logging("api", log_dir=tmp_path)
    configure_logging("api", log_dir=tmp_path)
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


def test_processes_write_separate_valid_jsonl(tmp_path):
    processes = [multiprocessing.Process(target=_writer, args=(str(tmp_path), service, 40))
                 for service in ("api", "orchestrator")]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    paths = list(tmp_path.glob("agent-trace-*.jsonl"))
    assert len(paths) == 2
    rows = [json.loads(line) for path in paths for line in path.read_text().splitlines()]
    assert len(rows) == 80
    assert {row["service"] for row in rows} == {"api", "orchestrator"}


def test_large_input_is_bounded_and_unknown_events_are_rejected(tmp_path):
    configure_logging("api", log_dir=tmp_path)
    emit_agent_event("model.request", input={"messages": ["x" * 1000] * 20})
    row = json.loads(next(tmp_path.glob("*.jsonl")).read_text().splitlines()[-1])
    assert row["input"]["truncated"] is True
    assert row["input"]["length"] > 8192
    with pytest.raises(ValueError):
        emit_agent_event("arbitrary.event")
