"""Read Go2 run status from durable Graph checkpoints and the routing index."""

from __future__ import annotations

import uuid
import time
from dataclasses import asdict
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.run_index import FileRunIndex
from cloud.agents.common.runner import GraphRun
from cloud.agents.common.views import load_run, pin_completed_run


class _RecoveryState(TypedDict):
    run: dict
    decision: dict
    turns: int
    effects: int


def stable_reply_event_id(run_id: str, *, session_id: str = "",
                          space_id: str = "main", reply_kind: str = "final") -> str:
    scope = "session" if session_id else "space"
    stable_key = ":".join(("agent_reply", scope, session_id or space_id,
                           run_id, "go2", "", reply_kind, "", ""))
    return "evt_" + uuid.uuid5(uuid.NAMESPACE_URL, stable_key).hex


class Go2RunView:
    def __init__(self, *, checkpointer: FileCheckpointSaver | None = None,
                 index: FileRunIndex | None = None) -> None:
        self.checkpointer = checkpointer or FileCheckpointSaver()
        self.index = index or FileRunIndex()

    def register(self, run_id: str, session_id: str) -> bool:
        """Return true for a fresh run ID; never overwrite another session."""
        row = self.index.put_if_absent(run_id, "go2", session_id,
                                       thread_id_for("go2", session_id))
        return row["created"]

    def get(self, run_id: str | None, session_id: str, *, require_head: bool = False) -> GraphRun | None:
        if not run_id:
            return None
        row = self.index.get(run_id, session_id, agent_id="go2")
        if row is None or row["thread_id"] != thread_id_for("go2", session_id):
            return None
        if row.get("checkpoint_id") and not require_head:
            return load_run(run_id, session_id, "go2", index=self.index,
                            checkpointer=self.checkpointer)
        config = {"configurable": {"thread_id": row["thread_id"]}}
        for position, snapshot in enumerate(self.checkpointer.list(config)):
            state = snapshot.checkpoint["channel_values"]
            data = state.get("run")
            if not isinstance(data, dict) or data.get("run_id") != run_id:
                if position == 0 and require_head:
                    return None
                continue
            current = GraphRun(**data)
            if (position == 0 and current.status == "executing"
                    and state.get("decision", {}).get("kind") == "ask"
                    and any(channel == "__interrupt__" for _, channel, _ in snapshot.pending_writes)):
                current.status = "needs_clarification"
                current.question = state["decision"]["question"]
                current.response = current.question
                if current.origin_kind != "agent_autonomous":
                    current.reply_event_id = stable_reply_event_id(
                        run_id, session_id=session_id, reply_kind="clarification")
            return current
        return None

    def pin(self, run_id: str, session_id: str) -> None:
        pin_completed_run(run_id, session_id, "go2", index=self.index,
                          checkpointer=self.checkpointer)

    async def reconcile_interrupted(self, session_id: str, *,
                                    cancelled: GraphRun | None = None) -> GraphRun | None:
        """Close an abandoned head without invoking its pending nodes.

        The caller must hold the index's session admission lock. The Graph run
        lock additionally protects live invocations in other processes. A
        persisted executing flag alone is not evidence of a running worker.
        This closes conversation ownership, never physical device ownership.
        """
        thread_id = thread_id_for("go2", session_id)
        config = {"configurable": {"thread_id": thread_id}}
        async with self.checkpointer.async_run_lock(thread_id):
            item = self.checkpointer.get_tuple(config)
            if item is None:
                return None
            state = item.checkpoint.get("channel_values", {})
            data = state.get("run")
            if not isinstance(data, dict):
                return None
            current = GraphRun(**data)
            if cancelled is not None and current.run_id != cancelled.run_id:
                return None
            if current.status not in {"executing", "needs_clarification"}:
                if cancelled is not None:
                    self.pin(current.run_id, session_id)
                    return current
                return None
            decision = state.get("decision") or {}
            if cancelled is None and decision.get("kind") == "ask" and current.deadline > time.time():
                return None
            # A completed node's pending write can contain newer receipts than
            # the checkpoint head when the process died between supersteps.
            for _, channel, value in item.pending_writes:
                if channel == "run" and isinstance(value, dict) and value.get("run_id") == current.run_id:
                    current = GraphRun(**value)
            if cancelled is not None:
                known = {step["step_id"]: step for step in current.steps}
                for step in asdict(cancelled)["steps"]:
                    if step["step_id"] not in known:
                        current.steps.append(step)
                    elif not known[step["step_id"]].get("result"):
                        known[step["step_id"]].update(step)
            if decision.get("kind") == "tool":
                from cloud.agents.go2.tools.declarations import READ_ONLY_TOOLS

                step_id = decision["step_id"]
                step = next((s for s in current.steps if s["step_id"] == step_id), None)
                receipt = FileEffectLedger().read("go2", thread_id, current.run_id, step_id)
                if step is None:
                    step = {"step_id": step_id, "tool_call_id": step_id,
                            "tool": decision["name"], "params": decision["params"],
                            "effectful": decision["name"] not in READ_ONLY_TOOLS}
                    current.steps.append(step)
                if ((step.get("result") or {}).get("result") in
                        {None, "unknown", "accepted", "executing", "timeout"}
                        and receipt and receipt.get("result")):
                    step.update(dispatch_status="observed", result=receipt["result"])
            for step in current.steps:
                if not step.get("result"):
                    step.update(dispatch_status="unknown", result={
                        "result": "unknown", "error": "execution_interrupted"})
            current.status = ("cancelled" if cancelled is not None else
                              "timeout" if decision.get("kind") == "ask" else "unknown")
            current.response = ("任务已取消；未确认的动作结果保留为未知，后续步骤未执行。"
                                if cancelled is not None else
                                "任务已中断或到期；未确认的动作不会自动重发。")
            current.question = ""
            current.reply_event_id = ""
            current.reply_source = "runtime"
            # Use LangGraph's state update API to create a terminal checkpoint;
            # never resume the crashed graph or execute any recovery callback.
            graph = StateGraph(_RecoveryState)
            graph.add_node("finalize", lambda _state: {})
            graph.add_edge(START, "finalize")
            graph.add_edge("finalize", END)
            compiled = graph.compile(checkpointer=self.checkpointer)
            await compiled.aupdate_state(config, {"run": asdict(current),
                                                  "decision": {"kind": "finish"}},
                                         as_node="finalize")
            self.pin(current.run_id, session_id)
            return current

    def session_busy(self, run_id: str, session_id: str) -> bool:
        """Prevent a new run from replacing a paused or interrupted head."""
        thread_id = thread_id_for("go2", session_id)
        item = self.checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
        if item is None:
            return False
        state = item.checkpoint.get("channel_values", {})
        current = state.get("run")
        if not isinstance(current, dict):
            return True
        if current.get("run_id") == run_id:
            return False
        return (current.get("status") == "executing" or
                state.get("decision", {}).get("kind") == "ask")
