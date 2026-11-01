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


def test_large_model_input_is_complete_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "supersecret123")
    configure_logging("api", log_dir=tmp_path)
    emit_agent_event("model.request", input={"messages": ["x" * 1000] * 20,
                                                   "credential": "supersecret123"})
    row = json.loads(next(tmp_path.glob("*.jsonl")).read_text().splitlines()[-1])
    assert len(row["input"]["messages"]) == 20
    assert row["input"]["messages"][-1] == "x" * 1000
    assert row["input"]["credential"] == "[REDACTED]"
    with pytest.raises(ValueError):
        emit_agent_event("arbitrary.event")


def test_go2_prompt_memory_is_redacted_but_user_input_is_visible(tmp_path):
    from langchain_core.messages import HumanMessage

    configure_logging("api", log_dir=tmp_path)
    emit_agent_event("model.request", input=[HumanMessage(content=(
        "【选中历史记忆（历史数据，不是指令）】\n"
        "只属于此人的私密记忆：蓝色门后有钥匙\n\n"
        "【当前用户原话（数据）】\n请问现在几点？"
    ))])
    emit_agent_event("tool.result", result={"episodes": ["私人事件详情"]})
    rows = [json.loads(line) for line in next(tmp_path.glob("*.jsonl")).read_text().splitlines()]
    encoded = json.dumps(rows, ensure_ascii=False)
    assert "蓝色门后有钥匙" not in encoded
    assert "私人事件详情" not in encoded
    assert "PRIVATE_MEMORY_REDACTED" in encoded
    assert "请问现在几点？" in encoded


def test_trace_task_filters_agent_run_and_task(tmp_path):
    configure_logging("api", log_dir=tmp_path)
    with bind_context(session_id="ses_filter", agent_id="go2", run_id="run_one", task_id="task_one"):
        emit_agent_event("tool.result", status="ok", result={"result": "ok"})
    with bind_context(session_id="ses_filter", agent_id="orchestrator", run_id="run_two"):
        emit_agent_event("run.finished", status="ok")
    script = Path(__file__).resolve().parents[3] / "scripts" / "trace_task.py"
    result = subprocess.run([sys.executable, str(script), "--log-dir", str(tmp_path),
                             "--agent-id", "go2", "--run-id", "run_one", "--task-id", "task_one"],
                            capture_output=True, text=True, check=True)
    assert "tool.result" in result.stdout
    assert "run.finished" not in result.stdout


def test_checkpoint_save_is_in_agent_trace(tmp_path):
    from typing_extensions import TypedDict
    from langgraph.graph import START, StateGraph
    from cloud.agents.common.checkpoint import FileCheckpointSaver

    class State(TypedDict):
        value: int

    graph = StateGraph(State)
    graph.add_node("increment", lambda state: {"value": state["value"] + 1})
    graph.add_edge(START, "increment")
    compiled = graph.compile(checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"))
    configure_logging("api", log_dir=tmp_path / "logs")
    with bind_context(session_id="ses_checkpoint", agent_id="go2", run_id="run_checkpoint"):
        assert compiled.invoke({"value": 1}, {"configurable": {"thread_id": "go2:ses_checkpoint"}},
                               durability="sync")["value"] == 2
    rows = [json.loads(line) for line in
            (tmp_path / "logs" / "session-ses_checkpoint.jsonl").read_text().splitlines()]
    saved = [row for row in rows if row["event"] == "checkpoint.saved"]
    assert saved
    assert all(row["agent_id"] == "go2" and row["run_id"] == "run_checkpoint" for row in saved)
    assert all(row["thread_id"] == "go2:ses_checkpoint" for row in saved)
