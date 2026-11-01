"""Isolated LangGraph motivation tick for Go2 free exploration.

The existing navigator still owns map frontier choice, collision checks,
lease, interruption and physical movement. This Graph only decides when an
explore or nearby social opportunity is due.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from cloud.agents.go2.personality.behavior import exploration_interval

SOCIAL_CHECK_INTERVAL = 5


class MotivationState(TypedDict, total=False):
    state: str
    curiosity: int
    social_tick: int
    person_engaging: bool
    interrupted: bool
    pending_question: bool
    connected: bool
    should_explore: bool
    should_social: bool
    profile_revision: int


def build_motivation_graph(*, profile_loader, checkpointer: Any = None):
    """Move the prior Drive tick policy into a typed, checkpointable node."""
    async def advance(state: MotivationState) -> MotivationState:
        if state.get("interrupted") or state.get("pending_question") or not state.get("connected"):
            return {"should_explore": False, "should_social": False}
        profile = profile_loader()
        curiosity = state.get("curiosity", 0) + 1
        social_tick = state.get("social_tick", 0) + 1
        threshold = exploration_interval(profile["traits"]["curiosity"])
        if curiosity >= threshold:
            return {"state": "EXPLORING", "curiosity": 0, "social_tick": social_tick,
                    "should_explore": True, "should_social": False,
                    "profile_revision": profile["revision"]}
        if state.get("person_engaging") and social_tick >= SOCIAL_CHECK_INTERVAL:
            # Drive keeps its public state IDLE during this short social check.
            return {"state": "IDLE", "curiosity": curiosity, "social_tick": 0,
                    "should_explore": False, "should_social": True,
                    "profile_revision": profile["revision"]}
        return {"state": "IDLE", "curiosity": curiosity, "social_tick": social_tick,
                "should_explore": False, "should_social": False,
                "profile_revision": profile["revision"]}

    graph = StateGraph(MotivationState)
    graph.add_node("advance", advance)
    graph.add_edge(START, "advance")
    graph.add_edge("advance", END)
    return graph.compile(checkpointer=checkpointer)
