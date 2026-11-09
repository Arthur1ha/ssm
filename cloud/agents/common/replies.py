"""Facts for a persona reply after the tool loop stops without a safe final."""

from __future__ import annotations

import re

from cloud.agents.common.runner import GraphRun as Run


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
    """Report observed results when a role's reply model is unavailable."""
    role = "go2" if agent_id == "go2" else ("lamp" if agent_id.startswith("esp32") else "orchestrator")
    if role == "lamp" and status == "error" and run is not None and not run.steps:
        return "哼，这回我处理请求出了问题，还没做任何动作。稍后再试。"
    if run is not None and run.steps:
        results = [(step, step.get("result") or {}) for step in run.steps]
        completed = [step for step, result in results
                     if step.get("effectful", True) and result.get("result") == "ok"]
        failed = [result for _, result in results if result.get("result") in
                  {"blocked", "rejected", "error", "timeout", "unknown", "cancelled"}]
        offline = any(result.get("device_connected") is False
                      or result.get("device_fsm") == "offline"
                      or result.get("error") in {"device_precondition", "robot_disconnected", "offline"}
                      or any((item.get("result") or {}).get("device_connected") is False
                             or (item.get("result") or {}).get("device_fsm") == "offline"
                             for item in (result.get("tool_results") or [])
                             if isinstance(item, dict))
                      for _, result in results)
        if role == "go2" and (offline or status == "offline"):
            opening = "汪，邀请我收到了，但机身还没连上。" if "邀请" in run.goal else "汪，你的话我听到了，但机身还没连上。"
            return (opening
                    + ("前面的步骤有结果了，后面的动作这次做不了。" if completed
                       else "这次动作没能执行。"))
        if failed:
            reason = failed[-1].get("message") or failed[-1].get("error") or ""
            reason = reason if isinstance(reason, str) and reason and len(reason) <= 80 else ""
            if completed:
                prefix = {"go2": "汪，前面的动作完成了，后面的没做成。",
                          "lamp": "前一步已经到位，后一步没做成。",
                          "orchestrator": "部分安排已完成，其余未完成。"}[role]
            else:
                prefix = {"go2": "汪，这次动作还没做成。",
                          "lamp": "这次没调成。",
                          "orchestrator": "这次安排没能完成。"}[role]
            return prefix + (f"设备反馈：{reason}。" if reason else "")
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


def reply_facts(run: Run) -> dict:
    """Give each role the same observations without prescribing its voice."""
    return {"goal": run.goal, "status": run.status,
             "steps": [{"tool": step.get("tool"), "params": step.get("params"),
                        "result": step.get("result") or {"result": "unknown"}}
                       for step in run.steps],
             "decision_draft": run.decision_draft, "question": run.question,
             "runtime_note": run.response}
