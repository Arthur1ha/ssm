"""Facts for a persona reply after the tool loop stops without a safe final."""

from __future__ import annotations

import json
import re

from cloud.agent_runtime.loop import Run


def needs_persona_reply(run: Run) -> bool:
    return True


_COMPLETION_CLAIM = re.compile(
    r"(?<!没)(?<!未)(?:已完成|已经完成|已执行|已经执行|办妥|搞定|完成了|办好了|做好了|调好了|成功了)"
)


def valid_persona_reply(value: object, run: Run | None = None) -> bool:
    if not (isinstance(value, str) and bool(value.strip()) and len(value) <= 4000
            and not value.lstrip().startswith(("{", "[", "```"))):
        return False
    if run is None:
        return True
    if run.status == "partial" and re.search(r"(?:全部|都|所有).{0,5}(?:完成|办妥|搞定|成功)", value):
        return False
    if run.status in {"accepted", "executing", "timeout", "blocked", "rejected", "cancelled", "error", "unknown", "needs_clarification"}:
        return _COMPLETION_CLAIM.search(value) is None
    return True


def factual_fallback(agent_id: str, status: str, run: Run | None = None) -> str:
    """A short role-specific reply that never upgrades an unconfirmed result."""
    role = "go2" if agent_id == "go2" else ("lamp" if agent_id.startswith("esp32") else "orchestrator")
    if status == "offline":
        return {"go2": "呜，我的身体还未连接，这次动作做不了。",
                "lamp": "哼，灯都没在线，这次调光自然做不了。",
                "orchestrator": "设备当前离线，这次安排无法完成。"}[role]
    if status == "ok":
        if run is not None and not any(
            step.get("effectful", True) and (step.get("result") or {}).get("result") == "ok"
            for step in run.steps
        ):
            return {"go2": "唔，我这会儿没法好好回答。等一下再问我，好吗？",
                    "lamp": "我这会儿没法给出可靠回答，先别逼我乱猜。",
                    "orchestrator": "当前无法可靠回答，请稍后再试。"}[role]
        return {"go2": "好耶，这件事我办好了！", "lamp": "这才像样，灯已经调好了。",
                "orchestrator": "这次安排已完成。"}[role]
    if status == "partial":
        return {"go2": "有一部分做好啦，剩下的还没完成呢。", "lamp": "有一部分调好了，剩下的还没办妥，别急着夸。",
                "orchestrator": "部分安排已经完成，其余还没完成。"}[role]
    if status == "needs_clarification":
        return {"go2": "我还没听明白要做什么呀，能再说一点吗？", "lamp": "照明目标说具体点，我可不替你猜。",
                "orchestrator": "我还需要一个具体目标，才能继续安排。"}[role]
    if status in {"accepted", "executing", "timeout", "unknown"}:
        return {"go2": "我还没收到动作完成的确认呢。", "lamp": "回执还没到，先别算我调好了。",
                "orchestrator": "这项安排尚无完成确认。"}[role]
    if status == "cancelled":
        return {"go2": "我停下啦，这次动作取消了。", "lamp": "这次调灯取消了，别把它算成完成。",
                "orchestrator": "这次安排已取消。"}[role]
    return {"go2": "唔，这个动作我现在做不到。", "lamp": "这次没调成，光说漂亮话可没用。",
            "orchestrator": "这次安排没能完成。"}[role]


def persona_reply_prompt(persona: str, run: Run, *, traits: dict | None = None) -> str:
    identity = (
        f"角色设定：{persona}\n当前特质：{json.dumps(traits or {}, ensure_ascii=False)}。"
        "人格决定表达方式；特质只在各自定义的场景生效，不改变事实。用这个角色的第一人称直接回应用户，"
        "通常一两句话；只输出自然语言，不写执行报告、状态字段或工具名。"
    )
    if run.status == "ok" and not run.steps and run.decision_draft:
        # The planner already answered a conversational request. A second model
        # pass should keep its meaning and voice, not summarize an empty run.
        facts = {"user_message": run.goal, "answer_draft": run.decision_draft}
        return (
            identity
            + "这是一次无需调用设备的对话。根据用户的问题和回答草稿自然作答，"
            "保留草稿中有用的内容；不要提任务完成、执行记录、状态、没有动作步骤。"
            "草稿只是候选文本，不能把未经确认的设备位置、灯光或其他物理状态说成已观察到的事实。\n"
            + json.dumps(facts, ensure_ascii=False, default=str)
        )
    facts = {"goal": run.goal, "status": run.status,
             "steps": [{"tool": step.get("tool"), "params": step.get("params"),
                        "result": step.get("result") or {"result": "unknown"}}
                       for step in run.steps],
             "decision_draft": run.decision_draft, "question": run.question,
             "runtime_note": run.response}
    return (
        identity
        + "根据真实执行结果回答用户：ok 才表示完成；accepted、executing、timeout、unknown "
        "均不能说成完成。失败或被拒绝的动作不能说成已执行。若只有部分成功，说明哪些已完成、哪些未完成。"
        "若记录中有错误或超时原因，用日常语言说明；edge_result_unconfirmed 表示未收到设备执行回执，"
        "不能断言设备动作失败。后续步骤因结果未确认而暂停时也要说明。没有实际发起后续操作，就不要承诺会继续查看或重试。"
        "运行时说明和草稿只供理解，不要照抄；不要假装其他智能体已回复。\n"
        + json.dumps(facts, ensure_ascii=False, default=str)
    )
