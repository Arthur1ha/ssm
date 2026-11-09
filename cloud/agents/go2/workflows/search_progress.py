"""User-visible search facts derived from observations and actual graph stages."""


def _text(value, limit=240):
    return value.strip()[:limit] if isinstance(value, str) else ""


def search_progress_facts(stage, state, *, observation=None, candidate=None, receipt=None):
    observation = observation if observation is not None else state.get("observation") or {}
    visual = observation.get("observation") or {}
    target = _text(state.get("target"), 100)
    name = f"「{target}」" if target else "目标"
    summary = _text(visual.get("scene_summary"))
    proximity_reason = _text(visual.get("proximity_reason"), 160)
    proximity = visual.get("proximity")
    found = observation.get("found") is True
    candidate = candidate or state.get("candidate") or {}
    result = state.get("result") or {}
    reason = (receipt or {}).get("reason") or result.get("reason")
    basis = ""
    text = {
        "search_observing": f"正在观察当前位置，识别画面中的{name}。" if target else "正在观察当前位置。",
        "search_rechecking": f"上一画面显示{name}可能已在附近，正在用新画面复核。",
        "search_selecting": "正在结合刚才的画面与可通行区域，选择下一观察点。",
        "search_approach": "正在核对目标是否已在附近，并确认机身停稳。",
        "search_telemetry": "画面已观察，正在等待定位或地图反馈，以判断能否继续移动。",
    }.get(stage, "")
    if stage in {"search_observe", "search_candidate", "search_confirmed"}:
        text = ("" if not target else
                f"新画面再次识别到疑似{name}。" if stage == "search_confirmed" and found else
                f"画面中发现疑似{name}。" if found else f"这次画面暂未识别到{name}，还不能判断其他区域。")
        if summary:
            text += f"画面观察：{summary}"
        elif not target:
            text = "本次画面暂无可用的场景描述。"
        if found:
            text += {"nearby": " 视觉判断可能已在附近，仍需复核与停止确认。",
                     "far": " 视觉判断还需要继续靠近。"}.get(proximity, " 暂时无法判断是否已在附近。")
            if proximity_reason:
                text += f" 依据：{proximity_reason}"
    elif stage == "search_selected":
        basis = {
            "target_reobservation": "上一画面提供了疑似目标的方向，已选定沿该方向可达的观察点。",
            "frontier": "当前视野仍有未检查区域，已选定可达的边界观察点。",
            "observation_reposition": "当前转身观察的空间不足，已选定向前换位的观察点。",
            "visual_coverage": "当前视野尚未覆盖全部区域，已选定能补充观察的可达位置与朝向。",
            "pending_recheck": "地图中仍有待复核区域，已选定能重新查看该区域的可达观察点。",
        }.get(candidate.get("source"), "已根据当前地图选定下一处可达的观察位置。")
        if candidate.get("source") == "target_reobservation" and candidate.get("view_kind") == "turn":
            basis = "上一画面提供了疑似目标的方向，已选定原地转向后重新观察。"
        elif candidate.get("source") == "target_reobservation" and candidate.get("view_kind") == "lateral":
            basis = "需要换个角度复核疑似目标，已选定可通行的侧移观察位置。"
        text = basis + " 接下来执行选定动作，停稳后重新观察。"
    elif stage == "search_navigate":
        text = (f"正在原地转向疑似{name}的方向。" if candidate.get("view_kind") == "turn" else
                "正在侧移到选定的观察位置。" if candidate.get("view_kind") == "lateral" else
                f"正在沿选定路径分段靠近疑似{name}。" if candidate.get("source") == "target_reobservation"
                else "正在前往选定的下一观察点。") + "停稳后会重新观察。"
    elif stage == "search_move_result":
        action = "本次转向" if candidate.get("view_kind") == "turn" else "本段移动"
        text = (f"{action}已完成，接下来重新观察。" if (receipt or {}).get("result") == "ok"
                else f"{action}未确认完成，正在核对受阻原因。")
        detail = {
            "localization_unavailable": "定位反馈暂不可用。",
            "body_clearance_unavailable": "尚未确认机身周围有足够的通行空间。",
            "turn_clearance_unavailable": "尚未确认有足够的转身空间。",
            "path_not_found": "当前未找到可通行路径。",
            "goal_blocked": "选定的观察位置受阻。",
            "stop_unconfirmed": "尚未确认机身停稳。",
            "motion_not_observed": "未观察到足够的实际位移或转动。",
            "observation_turn_unconfirmed": "尚未确认已转到选定方向。",
            "observation_move_unconfirmed": "尚未确认已移动到选定位置。",
        }.get(reason, "")
        if detail:
            text += detail
    elif stage == "search_finished":
        text = _text(result.get("message"), 600)
    return {"text": text, "decision_summary": basis,
            "observation": {"found": found, "frame_id": observation.get("frame_id"),
                            "scene_summary": summary, "proximity_reason": proximity_reason,
                            "proximity": proximity if proximity in {"nearby", "far", "uncertain"} else "uncertain"},
            "reason": reason}
