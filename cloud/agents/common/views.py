"""Read a Graph run by its public ID without maintaining a second run journal."""

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.run_index import FileRunIndex
from cloud.agents.common.runner import GraphRun


def load_run(run_id: str, session_id: str, agent_id: str, *,
             index: FileRunIndex | None = None,
             checkpointer: FileCheckpointSaver | None = None) -> GraphRun | None:
    index = index or FileRunIndex()
    route = index.get(run_id, session_id, agent_id=agent_id)
    if route is None:
        return None
    saver = checkpointer or FileCheckpointSaver()
    config = {"configurable": {"thread_id": route["thread_id"]}}
    if route.get("checkpoint_id"):
        config["configurable"]["checkpoint_id"] = route["checkpoint_id"]
        items = [saver.get_tuple(config)]
    else:
        items = saver.list(config)
    for item in items:
        if item is None:
            continue
        run = item.checkpoint.get("channel_values", {}).get("run")
        if isinstance(run, dict) and run.get("run_id") == run_id:
            current = GraphRun(**run)
            decision = item.checkpoint.get("channel_values", {}).get("decision") or {}
            interrupted = any(channel == "__interrupt__"
                              for _, channel, _ in item.pending_writes)
            if decision.get("kind") == "ask" and current.status == "executing" and interrupted:
                current.status = "needs_clarification"
                current.question = decision.get("question") or ""
                current.response = current.question
                current.reply_ready = True
            return current
    return None


def pin_completed_run(run_id: str, session_id: str, agent_id: str, *,
                      index: FileRunIndex | None = None,
                      checkpointer: FileCheckpointSaver | None = None) -> dict:
    """Pin the latest checkpoint only when its run identity matches exactly."""
    index = index or FileRunIndex()
    route = index.get(run_id, session_id, agent_id=agent_id)
    if route is None:
        raise ValueError("run_not_found")
    saver = checkpointer or FileCheckpointSaver()
    item = saver.get_tuple({"configurable": {"thread_id": route["thread_id"]}})
    run = item.checkpoint.get("channel_values", {}).get("run") if item else None
    if not isinstance(run, dict) or run.get("run_id") != run_id:
        raise ValueError("run_checkpoint_mismatch")
    if run.get("status") in {"executing", "needs_clarification"}:
        raise ValueError("run_not_finished")
    checkpoint_id = item.config["configurable"]["checkpoint_id"]
    return index.mark_checkpoint(run_id, session_id, checkpoint_id)
