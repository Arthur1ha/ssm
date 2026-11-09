"""Best-effort persona text for observed progress, without delaying dispatch."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from cloud.agents.common.replies import valid_persona_reply
from cloud.agents.common.messages import observed_tool_messages, role_messages
from cloud.agents.common.runner import GraphRun

logger = logging.getLogger(__name__)
_slots = threading.BoundedSemaphore(2)


def progress_messages(persona: str, goal: str, step: dict):
    instructions = (f"{persona}\n根据这条已经发生的结构化进展，以自己的身份给用户写一句简短进展。"
            "只输出自然语言。ok 才有完成证据；accepted、executing、timeout、unknown "
            "都不能说成完成。教学草案成功不是物理动作完成。"
            "不要编造其他动作或其他智能体的台词。")
    return role_messages(instructions, goal) + observed_tool_messages([step])


def submit_progress(persona: str, goal: str, step: dict,
                    invoke: Callable[[list], str], publish: Callable[[str], None],
                    closed: threading.Event, deadline: float) -> None:
    """Drop excess or late narration; factual progress was already published."""
    slots = _slots
    if closed.is_set() or time.time() >= deadline or not slots.acquire(blocking=False):
        return
    messages = progress_messages(persona, goal, dict(step))
    observed = GraphRun("progress", "", goal, deadline, steps=[step],
                        status=(step.get("result") or {}).get("result", "unknown"))

    def work() -> None:
        try:
            answer = invoke(messages)
            if (not closed.is_set() and time.time() < deadline
                    and valid_persona_reply(answer, observed)):
                publish(answer.strip())
        except Exception as exc:
            logger.debug("persona progress unavailable: %s", type(exc).__name__)
        finally:
            slots.release()

    threading.Thread(target=work, daemon=True, name="persona-progress").start()
