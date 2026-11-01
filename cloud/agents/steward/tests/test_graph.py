"""Steward Graph keeps the intent contract with a native tool-call model."""

import time
import json
import os
import subprocess
import sys

from langchain_core.messages import AIMessage

from cloud.agents.steward import graph as steward
from cloud.agents.steward.skills import coordination


class FakeModel:
    def __init__(self):
        self.calls = 0
        self.bound_tools = []

    def bind_tools(self, tools, **_kwargs):
        self.bound_tools = tools
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(content="", tool_calls=[{
                "name": "read_state", "args": {}, "id": "read-1",
            }])
        return AIMessage(content="灯的状态已读取。")

    def invoke(self, _messages):
        return AIMessage(content="灯的状态已读取。")


def test_steward_graph_reads_state_then_replies(monkeypatch, tmp_path):
    monkeypatch.setenv("SSM_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    monkeypatch.setenv("SSM_RUN_INDEX_DIR", str(tmp_path / "run_index"))
    monkeypatch.setattr(steward, "build_messages", lambda *_args: ["目标：查看状态"])
    monkeypatch.setattr(steward, "needs_persona_reply", lambda _run: True)
    monkeypatch.setattr(steward, "persona_reply_prompt", lambda *_args, **_kwargs: "简述状态")
    monkeypatch.setattr(steward, "valid_persona_reply", lambda *_args: True)
    monkeypatch.setattr(steward, "submit_progress", lambda *_args: None)
    monkeypatch.setattr(steward.transport, "do_publish_feedback", lambda *_args, **_kwargs: None)
    events = []
    monkeypatch.setattr(steward.transport, "do_publish_ui_event", lambda _sid, event, **fields: events.append((event, fields)))
    monkeypatch.setattr(steward.transport, "_state", None)
    model = FakeModel()

    result = steward.run_intent_loop({
        "session_id": "test-session", "event_id": "intent-1", "user_msg": "查看状态",
        "actor": "admin", "persist": False, "expires_at": time.time() + 40,
    }, llm=model)

    assert result["result"] == "ok"
    assert len(result["steps"]) == 1
    assert result["steps"][0]["tool"] == "read_state"
    assert result["steps"][0]["result"]["result"] == "ok"
    assert model.calls == 3
    assert any(item["function"]["name"] == "read_state" for item in model.bound_tools)
    assert any(event == "agent_reply" for event, _fields in events)


def test_clarification_resume_and_duplicate_replay_across_processes(monkeypatch, tmp_path):
    monkeypatch.setenv("SSM_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    monkeypatch.setenv("SSM_RUN_INDEX_DIR", str(tmp_path / "run_index"))
    code = """
import json, sys, time
from langchain_core.messages import AIMessage
from cloud.agents.steward import graph as steward
mode = sys.argv[1]
class Model:
    def __init__(self): self.calls = 0
    def bind_tools(self, tools, **kwargs): return self
    async def ainvoke(self, messages):
        self.calls += 1
        if mode == 'start':
            return AIMessage(content='', tool_calls=[{'name':'ask_user','args':{'question':'要哪盏灯？'},'id':'ask-1'}])
        if mode in ('new', 'replay'): raise AssertionError('unexpected model call')
        return AIMessage(content='管家答复')
steward.build_messages = lambda *_args: ['任务']
steward.get_profile = lambda: {'traits': {}, 'revision': 1}
steward.current_persona = lambda *_args: '沉稳的管家'
steward.persona_reply_prompt = lambda *_args, **_kwargs: '请回答'
steward.valid_persona_reply = lambda *_args: True
steward.submit_progress = lambda *_args: None
steward.transport.do_publish_feedback = lambda *_args, **_kwargs: None
steward.transport.do_publish_ui_event = lambda *_args, **_kwargs: None
context = {'session_id':'cross-session', 'event_id':'evt-new', 'user_msg':'安排灯',
           'actor':'admin', 'persist':False, 'expires_at':time.time()+60}
if mode in ('resume', 'replay'): context['resume_run_id'] = 'evt-first'
elif mode == 'start': context['event_id'] = 'evt-first'
model = Model()
result = steward.run_intent_loop(context, llm=model, resume=mode == 'resume',
                                 supplemental='桌面灯' if mode == 'resume' else '')
print(json.dumps({'result':result, 'calls':model.calls}, ensure_ascii=False))
"""
    def invoke(mode):
        completed = subprocess.run([sys.executable, "-c", code, mode],
                                   cwd=os.getcwd(), capture_output=True, text=True,
                                   timeout=20, check=True)
        return json.loads(completed.stdout)

    started = invoke("start")
    assert started["result"]["result"] == "needs_clarification"
    assert started["result"]["question"] == "要哪盏灯？"
    other = invoke("new")
    assert other["result"]["result"] == "rejected"
    assert other["result"]["error"] == "session_busy"
    resumed = invoke("resume")
    assert resumed["result"]["result"] == "ok"
    assert resumed["result"]["response"] == "管家答复"
    replayed = invoke("replay")
    assert replayed["result"]["response"] == "管家答复"
    assert replayed["calls"] == 0


def test_empty_model_reply_fails_without_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("SSM_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    monkeypatch.setenv("SSM_RUN_INDEX_DIR", str(tmp_path / "run_index"))
    monkeypatch.setattr(steward, "build_messages", lambda *_args: ["目标：查看状态"])
    monkeypatch.setattr(steward, "needs_persona_reply", lambda _run: False)
    monkeypatch.setattr(steward.transport, "do_publish_feedback", lambda *_a, **_k: None)
    monkeypatch.setattr(steward.transport, "do_publish_ui_event", lambda *_a, **_k: None)

    class EmptyModel:
        def bind_tools(self, _tools, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            return AIMessage(content="")

    result = steward.run_intent_loop({
        "session_id": "empty-session", "event_id": "empty-intent",
        "user_msg": "查看状态", "actor": "admin", "persist": False,
        "expires_at": time.time() + 30,
    }, llm=EmptyModel())
    assert result["result"] == "error"
    assert result["steps"] == []


def test_peer_skill_keeps_signed_origin_and_child_result(monkeypatch):
    from contextlib import nullcontext

    seen = []
    monkeypatch.setattr(coordination.transport, "_state", None)
    monkeypatch.setattr(coordination.transport, "peer_feedback_scope", nullcontext)

    def run(context):
        seen.append(context)
        return {"result": "partial", "trace_id": "trace-child", "run_id": "child-run",
                "response": "设备结果未确认", "steps": [{
                    "step_id": "step-1", "tool": "invoke_skill",
                    "result": {"result": "timeout", "task_id": "task-1"}}]}

    monkeypatch.setattr(coordination, "run_intent_loop", run)
    trusted = {"request_id": "peer-1", "initiator_unit_id": "go2",
               "target_unit_id": "cloud_orchestrator", "origin_kind": "agent_autonomous",
               "actor": "agent", "space_id": "main", "expires_at": time.time() + 30,
               "params": {"message": "请查看状态"}, "trace_id": "trace-parent",
               "parent_run_id": "parent-run"}
    result = coordination.run_peer_loop(trusted)
    assert result["result"] == "timeout"
    assert result["task_results"]["step-1"]["task_id"] == "task-1"
    assert seen[0]["persist"] is False
    assert seen[0]["origin_kind"] == "agent_autonomous"
    assert seen[0]["parent_run_id"] == "parent-run"
