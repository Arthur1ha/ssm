"""Read Go2 run status from durable Graph checkpoints and the routing index."""

from __future__ import annotations

import uuid

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.run_index import FileRunIndex
from cloud.agents.common.runner import GraphRun
from cloud.agents.common.views import load_run, pin_completed_run


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
