"""Facts for a persona reply after the tool loop stops without a safe final."""

from __future__ import annotations

import json
import re

from cloud.agent_runtime.loop import Run


def needs_persona_reply(run: Run) -> bool:
    return run.status in {"partial", "accepted", "executing", "timeout", "unknown",
                          "blocked", "rejected", "cancelled", "error"}


def valid_persona_reply(value: object) -> bool:
    return (isinstance(value, str) and bool(value.strip())
            and not value.lstrip().startswith(("{", "[", "```"))
            and not re.search(r"第\s*[一二三四五六七八九十\d]+\s*步|已确认完成|部分步骤已执行", value))


def persona_reply_prompt(persona: str, run: Run) -> str:
    facts = {"goal": run.goal, "status": run.status,
             "steps": [{"tool": step.get("tool"), "params": step.get("params"),
                        "result": step.get("result") or {"result": "unknown"}}
                       for step in run.steps],
             "runtime_note": run.response}
    return (
        f"{persona}\n你正在以自己的身份向用户回复。只输出一句或两句自然口语，不输出 JSON 或步骤编号。"
        "根据下面的真实执行记录解释结果：ok 才表示完成；accepted、executing、timeout、unknown "
        "均不能说成完成。失败或被拒绝的动作不能说成已执行。若只有部分成功，说明哪些已完成、哪些未完成。"
        "不要把运行时说明照抄给用户，也不要假装其他智能体已回复。\n"
        + json.dumps(facts, ensure_ascii=False, default=str)
    )
