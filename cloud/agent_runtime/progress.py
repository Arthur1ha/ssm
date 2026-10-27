"""Best-effort persona text for observed progress, without delaying dispatch."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

from cloud.agent_runtime.replies import valid_persona_reply

logger = logging.getLogger(__name__)
_slots = threading.BoundedSemaphore(2)


def progress_prompt(persona: str, goal: str, step: dict) -> str:
    facts = {"goal": goal, "step_id": step.get("step_id"),
             "tool": step.get("tool"), "params": step.get("params"),
             "result": step.get("result") or {"result": "unknown"}}
    return (f"{persona}\n根据这条已经发生的结构化进展，以自己的身份给用户写一句简短进展。"
            "只输出自然语言。ok 才有完成证据；accepted、executing、timeout、unknown "
            "都不能说成完成。不要编造其他动作或其他智能体的台词。\n"
            + json.dumps(facts, ensure_ascii=False, default=str))


def submit_progress(persona: str, goal: str, step: dict,
                    invoke: Callable[[str], str], publish: Callable[[str], None],
                    closed: threading.Event, deadline: float) -> None:
    """Drop excess or late narration; factual progress was already published."""
    if closed.is_set() or time.time() >= deadline or not _slots.acquire(blocking=False):
        return
    prompt = progress_prompt(persona, goal, dict(step))

    def work() -> None:
        try:
            answer = invoke(prompt)
            if (not closed.is_set() and time.time() < deadline
                    and valid_persona_reply(answer)):
                publish(answer.strip())
        except Exception as exc:
            logger.debug("persona progress unavailable: %s", type(exc).__name__)
        finally:
            _slots.release()

    threading.Thread(target=work, daemon=True, name="persona-progress").start()
