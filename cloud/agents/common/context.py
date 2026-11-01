"""Trusted request context shared by the three cloud agents."""

from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    agent_id: str
    session_id: str
    run_id: str = ""
    trace_id: str = ""
    parent_run_id: str = ""
    source: str = "user"
    persistent: bool = False

    @property
    def thread_id(self) -> str:
        return thread_id_for(self.agent_id, self.session_id)


def thread_id_for(agent_id: str, session_id: str) -> str:
    """Return a stable, bounded thread ID without leaking session contents."""
    if not agent_id or not session_id:
        raise ValueError("agent_id and session_id are required")
    digest = sha256(f"{agent_id}\0{session_id}".encode()).hexdigest()
    return f"{agent_id}:{digest}"
