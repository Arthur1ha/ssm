"""Trusted game tasks enter the existing device brains.

This module supplies stage tools and execution constraints, not a model or an
agent loop. Rules remain in each game; the role Graph owns all reasoning,
receipts, retries, checkpoints and final wording.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable, get_args

from pydantic import BaseModel, Field, field_validator

from cloud.agents.common.emotions import EmotionModel
from cloud.space.execution import ExecutionContext

DECISION_TOOL = "submit_game_decision"


@dataclass
class RoleTask:
    instructions: str = ""
    task_context: Callable[[], dict] | None = None
    allowed_tools: frozenset[str] = frozenset()
    availability: Callable[[], bool] | None = None
    tool_guard: Callable | None = None
    tool_context: Callable | None = None
    execution_options: Callable | None = None
    result_transform: Callable | None = None
    initial_tool: tuple[str, dict] | None = None
    decision_schema: type[BaseModel] | None = None
    reply_required: bool = True
    terminal: bool = False
    scope_fields: dict = field(default_factory=dict)

    def available(self) -> bool:
        from .scope import game_scope_available, terminal_scope_available
        allowed = (terminal_scope_available() if self.terminal and self.decision_schema is not None
                   and self.allowed_tools == frozenset({DECISION_TOOL}) else game_scope_available())
        return allowed and (self.availability is None or self.availability())

    def guard(self, name: str, params: dict) -> dict | None:
        if not self.available():
            return {"result": "cancelled", "error": "game_context_closed", "final": True}
        if name not in self.allowed_tools:
            return {"result": "rejected", "error": "tool_not_allowed_for_task"}
        return self.tool_guard(name, params) if self.tool_guard else None

    def declarations(self, ordinary: dict) -> dict:
        if self.decision_schema is not None:
            return {DECISION_TOOL: self.decision_schema}
        return {name: schema for name, schema in ordinary.items() if name in self.allowed_tools}

    def submit(self, name: str, params: dict) -> dict | None:
        if name != DECISION_TOOL or self.decision_schema is None:
            return None
        # The common runner omits null business arguments. Restore a stage's
        # required nullable fields before running its domain validators again.
        nullable = {key: None for key, value in self.decision_schema.model_fields.items()
                    if key not in params and value.is_required()
                    and type(None) in get_args(value.annotation)}
        decision = self.decision_schema.model_validate({**nullable, **params}).model_dump()
        return {"result": "ok", "decision": decision, "final": True}

    def execution_context(self, name: str, params: dict, step_id: str):
        return self.tool_context(name, params, step_id) if self.tool_context else nullcontext()

    def options(self, name: str, params: dict, step_id: str) -> dict:
        return self.execution_options(name, params, step_id) if self.execution_options else {}

    def transform(self, name: str, params: dict, receipt: dict) -> dict:
        if self.result_transform is None:
            return receipt
        transformed = self.result_transform(name, params, dict(receipt))
        if not isinstance(transformed, dict) or transformed.get("result") != receipt.get("result"):
            raise ValueError("game_result_transform_changed_outcome")
        if transformed.get("result") in {"ok", "accepted"} and transformed.get("final") is True:
            # Only the trusted business completion predicate grants this;
            # neither a model's final argument nor an arbitrary tool success
            # can erase an unresolved subtask. Keep all attempts in the ledger.
            transformed["_task_completion_confirmed"] = True
        return transformed

    def settle_status(self, run) -> None:
        last = run.steps[-1] if run.steps else {}
        receipt = last.get("result") or {}
        if (receipt.get("_task_completion_confirmed") is True
                and receipt.get("result") == "ok" and receipt.get("final") is True
                and not any((step.get("result") or {}).get("result") in
                            {"unknown", "timeout", "executing"} for step in run.steps)):
            run.status = "ok"

    def messages(self, messages: list, run=None) -> list:
        from langchain_core.messages import SystemMessage
        from cloud.agents.common.messages import context_message
        if self.instructions:
            messages.insert(1, SystemMessage(content=self.instructions))
        if self.task_context is not None:
            facts = self.task_context()
            if (self.decision_schema is not None and run is not None
                    and isinstance(facts.get("space"), dict)):
                # The game adapter may supply a private current observation
                # needed by this role's stage. Validate selected references
                # against this exact server-owned model-turn snapshot.
                run.decision_space = deepcopy(facts["space"])
            messages.insert(2 if self.instructions else 1,
                            context_message({"game_task": facts}))
        return messages

    def finish_decision(self, run):
        """A submitted choice is already the role's response; no second model."""
        if self.decision_schema is None:
            if not self.reply_required:
                last = run.steps[-1] if run.steps else {}
                run.response = str((last.get("result") or {}).get("message") or "").strip()
                run.reply_source = "tool_receipt"
                run.reply_ready = True
                return True
            return False
        last = next((step for step in reversed(run.steps) if step.get("tool") == DECISION_TOOL), {})
        decision = (last.get("result") or {}).get("decision") or {}
        run.response = str(decision.get("speech") or "").strip() if self.available() else ""
        run.emotion = decision.get("emotion", "neutral")
        run.reply_source = "decision_model"
        run.reply_ready = True
        return True

    def event_fields(self) -> dict:
        return {**self.scope_fields, "context_scope": "process"}


def _scope_fields() -> dict:
    from .scope import current_game_scope
    scope = current_game_scope()
    if scope is None:
        return {}
    return {"activity_id": scope.activity_id, "game_instance_id": scope.invitation_id,
            "game_id": scope.game_id}


async def _run(role: str, goal: str, *, context: ExecutionContext, run_id: str,
               deadline: float, task: RoleTask, dispatch=None, on_step=None,
               parent_run_id: str | None = None) -> dict:
    if not task.available():
        return {"result": "cancelled", "error": "game_context_closed", "tool_results": []}
    if role == "go2":
        from cloud.agents.go2.graph import run_go2_loop
        return await run_go2_loop(context.session_id, goal, context=context,
            run_id=run_id, deadline=deadline, parent_run_id=parent_run_id,
            publish_reply=False, on_step=on_step, task_adapter=task)
    if role != "lamp":
        raise ValueError("unknown_game_role")
    from cloud.agents.common.runner import invoke_blocking
    from cloud.esp32.service import get_agent
    agent = get_agent()
    if agent is None:
        return {"result": "blocked", "error": "lamp_agent_unavailable", "tool_results": []}
    if dispatch is None:
        def dispatch(*_args, **_kwargs):
            raise PermissionError("game_physical_dispatch_unavailable")
    result = await invoke_blocking(agent.run_user_loop, goal, "esp32_desk_led", context,
        dispatch, run_id=run_id, deadline=deadline, parent_run_id=parent_run_id,
        task_adapter=task)
    result.setdefault("tool_results", result.get("steps") or [])
    return result


async def decide(role: str, phase: str, facts: dict, *, schema: type[BaseModel],
                 instructions: str, thread_id: str, deadline: float,
                 availability: Callable[[], bool] | None = None,
                 terminal: bool = False) -> dict:
    """Submit a typed stage choice through the device's ordinary role Graph."""
    identity = hashlib.sha256(f"{role}:{thread_id}:{phase}".encode()).hexdigest()
    context = ExecutionContext("peer_game_" + identity[:32], "visitor", False,
                               origin_kind="agent_autonomous", expires_at=int(deadline))
    task = RoleTask(instructions=instructions + (
        "\n这是同一个设备大脑中的当前游戏阶段。必须调用 submit_game_decision 提交本阶段选择，"
        "不要用纯文本替代结构化提交，不执行其他阶段。speech可省略时保持安静，"
        "不为内部确认复述规则或执行流程。"),
        task_context=lambda: facts, allowed_tools=frozenset({DECISION_TOOL}),
        availability=availability, decision_schema=schema, scope_fields=_scope_fields(),
        terminal=terminal)
    result = await _run(role, "请根据当前游戏阶段和真实事实作出选择。", context=context,
        run_id="game_decision_" + identity[:32], deadline=deadline, task=task)
    for step in reversed(result.get("tool_results") or result.get("steps") or []):
        receipt = step.get("result") or {}
        if step.get("tool") == DECISION_TOOL and receipt.get("result") == "ok":
            if not task.available():
                raise ValueError("game_context_closed")
            return schema.model_validate(receipt["decision"]).model_dump()
    raise ValueError("game_decision_unavailable:" + str(result.get("result", "unknown")))


async def run_device_task(role: str, goal: str, *, context: ExecutionContext,
                          run_id: str, deadline: float, allowed_tools,
                          tool_guard=None, task_context=None, instructions: str = "",
                          availability=None, dispatch=None, on_step=None,
                          parent_run_id: str | None = None, initial_tool=None,
                          tool_context=None, result_transform=None,
                          execution_options=None, share_result: bool = True,
                          reply_required: bool = True) -> dict:
    """Run one cooperative subtask and share only its final role-owned result."""
    task = RoleTask(instructions=instructions + (
        "\n在本次设备子任务内部处理可恢复的失败和重新观察，不向同伴逐步报告错误或重试。"
        "结果未知时等待或查询原任务，不换身份重复执行。只在完成或无法继续时用一句简短事实说明最终结果，"
        "不要邀请重玩或继续已经结束的游戏。"),
        task_context=task_context, allowed_tools=frozenset(allowed_tools),
        availability=availability, tool_guard=tool_guard, tool_context=tool_context,
        execution_options=execution_options, result_transform=result_transform,
        initial_tool=initial_tool, scope_fields=_scope_fields(), reply_required=reply_required)
    result = await _run(role, goal, context=context, run_id=run_id, deadline=deadline,
                      task=task, dispatch=dispatch, on_step=on_step,
                      parent_run_id=parent_run_id)
    from .scope import game_scope_available
    if share_result and task.scope_fields and game_scope_available():
        from cloud.space.activity import get_activity_store
        text = str(result.get("response") or result.get("reply") or "").strip()
        if not text:
            text = "本次设备子任务已完成。" if result.get("result") == "ok" else "本次设备子任务未确认完成。"
        fields = {**task.scope_fields, "context_scope": "shared", "run_id": run_id,
                  "parent_run_id": parent_run_id or "", "status": result.get("result", "unknown"),
                  "source_event_id": "game-task-result:" + role + ":" + run_id,
                  "fact_kind": "result", "result_kind": "action"}
        steps = result.get("tool_results") or result.get("steps") or []
        fields["public_result"] = (steps[-1].get("result") or {}) if steps else {
            "result": result.get("result", "unknown")}
        activity_id = fields.pop("activity_id")
        get_activity_store().record_event(activity_id, "task_result",
            unit_id="go2" if role == "go2" else "esp32_desk_led", text=text, **fields)
    return result


class GameFinal(EmotionModel):
    speech: str = Field(min_length=1, max_length=160,
                        description="本局结束时，以自己的口吻给出一句简短最终回应；不提问、不续局、不安排动作")

    @field_validator("speech")
    @classmethod
    def short_final(cls, value):
        value = value.strip()
        if not value or "\n" in value or len([
                part for part in re.split(r"[。！？!?]+", value) if part.strip()]) > 1:
            raise ValueError("one_short_final_response_required")
        return value


def _reusable_final(value: object, status: str) -> dict | None:
    if not isinstance(value, dict) or value.get("terminal") is not True:
        return None
    if (value.get("result") or value.get("status")) != status:
        return None
    text = str(value.get("speech") or value.get("response") or value.get("reply") or "").strip()
    # Reuse an already suitable final line exactly. A long response needs the
    # original role to formulate its final expression; never slice its meaning.
    if (not text or len(text) > 160 or "\n" in text
            or len([part for part in re.split(r"[。！？!?]+", text) if part.strip()]) > 1):
        return None
    return {"speech": text, "emotion": value.get("emotion", "neutral")}


async def final_responses(game, *, activity_id: str, invitation_id: str,
                          context: ExecutionContext, result: dict, deadline: float,
                          existing: dict | None = None) -> list[dict]:
    """Both original brains close this game with one line and no new effects."""
    from .scope import terminal_game_scope, terminal_scope_available
    prior = existing if existing is not None else result.get("role_replies") or {}
    status = str(result.get("result") or result.get("status") or "unknown")
    metadata = game.metadata()

    async def response(role):
        with terminal_game_scope(activity_id, invitation_id, game.game_id):
            if not terminal_scope_available():
                return None
            saved = _reusable_final(prior.get(role) or (
                prior.get("esp32_desk_led") if role == "lamp" else None), status)
            if saved is None:
                try:
                    saved = await decide(role, "final", {
                        "game": metadata, "stage": "game_final", "result": result,
                        "execution_context": {"space_id": context.space_id,
                                              "origin_kind": context.origin_kind}},
                        schema=GameFinal,
                        instructions=("本局已经关闭动作和讨论，现在只给出你自己的一句简短最终回应。"
                                      "依据本局真实终局结果，以自己的口吻接话；不复述规则、内部错误或执行过程。"
                                      "完成尝试不冒充送达，未知不冒充成功。无需重复对方的话。"
                                      "不要提出问题、邀请重试、续写目标或承诺之后行动；说完回到自由活动。"),
                        thread_id="game:" + invitation_id + ":final:" + role,
                        deadline=deadline, terminal=True)
                except (ValueError, TimeoutError):
                    # Model availability must not prevent lifecycle cleanup.
                    text = ("这局结束了，我回去自由活动啦。" if status == "ok" else
                            "这局就到这里，本次结果还没有确认完成。" if status in {
                                "unknown", "timeout", "executing"} else "这局没能完成，就到这里啦。")
                    saved = {"speech": text, "emotion": "neutral"}
            if terminal_scope_available():
                return {"role": role, **saved}
            return None

    responses = await asyncio.gather(response("lamp"), response("go2"))
    return [value for value in responses if value is not None]
