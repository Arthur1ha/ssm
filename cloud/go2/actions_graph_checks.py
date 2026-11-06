"""New actions must preserve the existing Graph's uncertainty and replay boundary."""

import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.run_index import FileRunIndex
from cloud.agents.go2 import graph
from cloud.agents.go2.memory.episode import EpisodeMemory
from cloud.agents.go2.run_view import Go2RunView
from cloud.go2.actions import ACTIONS
from cloud.go2.connection import go2
from cloud.go2.control import control_lease
from cloud.go2.execution import ExecutionContext


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "Sit", "RiseSit", "Content", "FingerHeart", "FrontPounce", "FrontJump",
])
async def test_new_action_acceptance_pauses_graph_and_does_not_replay(
        command, tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    view = Go2RunView(checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
                      index=FileRunIndex(tmp_path / "index"))
    monkeypatch.setattr(graph, "get_store", lambda: view)
    monkeypatch.setattr(graph, "episode_memory", EpisodeMemory(tmp_path / "episodes"))
    monkeypatch.setattr(graph, "recall_memory", lambda *_a, **_k:
                        SimpleNamespace(fragments=[], text=""))
    monkeypatch.setattr(graph, "go2_messages", lambda *_a, **_k:
                        [HumanMessage(content="perform the requested action, then dance")])
    monkeypatch.setattr("cloud.agents.go2.personality.profile.get_profile",
                        lambda: {"prompt": "Go2", "revision": 1})
    monkeypatch.setattr(graph, "needs_persona_reply", lambda _run: False)
    monkeypatch.setattr(graph, "emit_agent_event", lambda *_a, **_k: None)
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    monkeypatch.setattr("cloud.api.intents.publish_agent_event", lambda *_a, **_k: {})
    monkeypatch.setattr("cloud.api.policy.audit_event", lambda *_a, **_k: None)

    model_calls = []

    class Model:
        def bind_tools(self, _tools, **kwargs):
            assert kwargs["parallel_tool_calls"] is False
            return self

        async def ainvoke(self, messages):
            model_calls.append(messages)
            assert len(model_calls) == 1, "unconfirmed action must stop planning dependent actions"
            return AIMessage(content="", tool_calls=[{
                "name": "go2_sport", "args": {"cmd": command}, "id": "new-action",
            }])

    monkeypatch.setattr(graph, "get_text_llm", lambda: Model())
    publications = []

    async def publish(topic, options):
        publications.append((topic, options))
        return {"data": {"header": {"status": {"code": 0}}}}

    monkeypatch.setattr(go2, "is_connected", True)
    monkeypatch.setattr(go2, "_conn", SimpleNamespace(datachannel=SimpleNamespace(
        pub_sub=SimpleNamespace(publish_request_new=publish))))
    go2.runtime.body("sitting" if command == "RiseSit" else "standing", confirmation='observed')
    monkeypatch.setattr(go2, "_completion", None)
    monkeypatch.setattr(go2, "pending_action", None)
    monkeypatch.setattr(go2, "body_confirmation", "observed")
    owner = control_lease.snapshot()
    if owner is not None:
        control_lease.release(owner.owner_session_id)
    context = ExecutionContext("action-contract", "admin", True,
                               expires_at=int(time.time()) + 60)
    try:
        result = await graph.run_go2_loop(
            context.session_id, f"{command}, then Dance1", context=context,
            run_id="action-contract-run", publish_reply=False)
        assert result["result"] == "accepted"
        assert result["actions_taken"] == []
        device_result = result["tool_results"][0]["result"]
        assert device_result["confirmation_scope"] == "request"
        assert device_result["device_fsm"] is None
        assert len(publications) == 1
        assert publications[0][1]["api_id"] == ACTIONS[command].api_id
        repeated = await graph.run_go2_loop(
            context.session_id, f"{command}, then Dance1", context=context,
            run_id="action-contract-run", publish_reply=False)
        assert repeated == result
        assert len(publications) == 1
        assert len(model_calls) == 1
    finally:
        control_lease.release(context.session_id)
