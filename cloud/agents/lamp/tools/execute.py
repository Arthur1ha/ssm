"""Route a single validated graph tool call through existing ESP32 boundaries."""

from __future__ import annotations

from cloud.esp32.tool_executor import execute_auxiliary, execute_physical
from cloud.esp32.tool_registry import TOOL_DECLARATIONS
from cloud.esp32.physical_tasks import stable_task_id


def execute_lamp_tool(name: str, params: dict, step_id: str, *, agent, unit_id: str,
                      context, dispatch, lighting_only: bool,
                      run_id: str, decision_space: dict | None = None) -> dict:
    # Optional Pydantic fields may arrive as null; omission preserves the
    # existing device protocol's strict parameter contract.
    params = {key: value for key, value in params.items() if value is not None}
    declaration = TOOL_DECLARATIONS.get(name)
    if declaration is None or (lighting_only and not declaration.get("action")):
        return {"result": "rejected", "error": "tool_not_allowed"}
    if name == "request_peer_skill":
        params = {**params, "request_id": step_id}
    if name == "send_message" and context.origin_kind == "agent_autonomous":
        from cloud.peers.social import activity_peers
        from cloud.space.context import get_space_context
        space = get_space_context(unit_id, state=agent._state)
        activity, peers = activity_peers(unit_id, space["peers"])
        target = str(params.get("target") or "")
        topic = str(params.get("content") or "")
        social = agent._social_state
        if activity and social.activity_id != activity["activity_id"]:
            social.reset(activity["activity_id"])
        if (not activity or target not in {peer["unit_id"] for peer in peers}
                or not social.can_send(target, topic)):
            return {"result": "blocked", "error": "social_gate"}
        result = execute_auxiliary(unit_id=unit_id, tool=name, params=params, context=context)
        if result.get("result") == "accepted":
            social.sent(target, topic)
        return result
    if declaration.get("action"):
        decision_basis = None
        reason = params.pop("decision_reason", None)
        refs = params.pop("evidence_refs", None)
        game_setup = context.session_id.startswith("peer_game_color_")
        current_space = None
        if game_setup or reason is not None or refs is not None:
            from cloud.space.context import get_space_context
            from cloud.space.decisions import resolve_decision
            try:
                current_space = get_space_context(unit_id, state=agent._state)
                decision_basis = resolve_decision(
                    current_space, reason, refs,
                    require_observation=game_setup,
                    observed_space=decision_space)
            except ValueError as exc:
                return {"result": "rejected", "error": str(exc),
                        "execution_phase": "preflight"}
        from cloud.cards.registry import get_registry
        card = get_registry().get_card(unit_id)
        if not card:
            return {"result": "blocked", "error": "lamp_unavailable"}
        task_id = stable_task_id(unit_id, run_id, step_id)
        if game_setup:
            from cloud.peers.games import activity_allows
            from cloud.space.activity import get_activity_store
            store = get_activity_store()
            activity = store.current()
            activity_id = (current_space.get("activity") or {}).get("activity_id")
            if (not activity_id or (activity or {}).get("activity_id") != activity_id
                    or not activity_allows(activity_id, unit_id, "go2")
                    or not agent._should_act() or not agent._device_online()):
                return {"result": "blocked", "error": "game_activity_unavailable"}
            intent = ("我准备开灯，亮出这局的题目颜色。" if name == "set_led_state"
                      else "我准备亮出这局的题目颜色。")
            announced = store.record_event(
                activity_id, "action", unit_id=unit_id, text=intent,
                source_event_id="decision_" + task_id,
                action=name, task_id=task_id, game_id="color_hunt",
                trigger_message_id=context.session_id.removeprefix("peer_game_color_"),
                decision_basis=decision_basis, status="executing")
            if announced is None:
                return {"result": "cancelled", "error": "activity_ended"}
        result = execute_physical(
            state=agent._state, unit_id=unit_id,
            action=declaration["action"], params=params,
            context=context, dispatch=dispatch,
            source="lamp_semantic" if lighting_only else "lamp_chat",
            task_id=task_id, card=card,
        )
        if decision_basis is not None:
            result = {**result, "decision_basis": decision_basis}
        return result
    return execute_auxiliary(unit_id=unit_id, tool=name, params=params, context=context)
