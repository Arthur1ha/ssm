"""One bounded LangGraph for room exploration and object search.

Only scalar evidence references enter checkpoints. The owning HTTP chat task
awaits this graph to completion; it never hands motion to an orphan worker.
"""
from __future__ import annotations

import asyncio
from cloud.agents.common.runner import invoke_blocking
from contextlib import contextmanager
from contextvars import ContextVar
import math
import logging
import os
import time
import traceback
import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

SEARCH_BUDGET_S = 240.0
SEARCH_TOOL_TIMEOUT_S = 250.0
SEARCH_SERVER_TIMEOUT_S = 270.0
SEARCH_MAX_STEPS = 20
SEARCH_MAX_DISTANCE_M = 20.0
OBSERVATION_TIMEOUT_S = 35.0
OBSERVATION_SEGMENT_M = .80
OBSERVATION_SEGMENT_RAD = .80
OBSERVATION_SEGMENT_S = 5.0
TARGET_VIEW_ATTEMPTS = 4
_request: ContextVar[tuple[str, float] | None] = ContextVar("go2_search_request", default=None)
_configured_scope: tuple[tuple[float, float, float, float] | None, str] | None = None
_exploration_coverage = None


def configure_search_scope(bounds, session_id: str, *, mode: str = "bounded") -> dict:
    """Set an in-memory scope tied to one localization session; never move."""
    global _configured_scope, _exploration_coverage
    if not isinstance(session_id, str) or not session_id or mode not in {"bounded", "reachable"}:
        raise ValueError("invalid_search_scope")
    if mode == "reachable":
        if bounds is not None:
            raise ValueError("reachable_scope_has_no_rectangle")
        _configured_scope = (None, session_id)
    elif (not isinstance(bounds, (list, tuple)) or len(bounds) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in bounds)
            or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
        raise ValueError("invalid_search_scope")
    else:
        _configured_scope = (tuple(float(v) for v in bounds), session_id)
    _exploration_coverage = None
    return configured_search_scope()


def configured_search_scope() -> dict | None:
    scope = _configured_scope
    return {"bounds": list(scope[0]) if scope[0] is not None else None,
            "mode": "bounded" if scope[0] is not None else "reachable",
            "session_id": scope[1]} if scope else None


@contextmanager
def search_request(task_id: str, deadline: float):
    token = _request.set((task_id, deadline))
    try:
        yield
    finally:
        _request.reset(token)


def search_bounds() -> tuple[float, float, float, float] | None:
    """None means explicitly authorized reachable space, never an implicit default."""
    if _configured_scope is not None:
        return _configured_scope[0]
    try:
        values = tuple(float(v) for v in os.environ.get("GO2_SEARCH_BOUNDS", "").split(","))
        if len(values) != 4 or not all(math.isfinite(v) for v in values):
            raise ValueError
        if values[0] >= values[2] or values[1] >= values[3]:
            raise ValueError
        return values
    except ValueError as exc:
        raise ValueError("search_scope_required") from exc


def resolve_search_scope(context, mode: str, map_session: str) -> dict:
    """A user search owns a reachable scope for this task, never a global mode.

    Explicit rectangles remain authoritative and cannot cross odom sessions.
    Autonomous exploration still requires its separately configured scope.
    """
    user_search = mode == "find_object" and context.origin_kind == "user_delegated"
    configured = configured_search_scope()
    if configured is not None:
        if configured["session_id"] == map_session:
            return {**configured, "source": "configured"}
        if configured["mode"] != "reachable" or not user_search:
            raise ValueError("search_scope_session_mismatch")
    elif "GO2_SEARCH_BOUNDS" in os.environ or "GO2_SEARCH_SESSION_ID" in os.environ:
        bounds = search_bounds()
        if os.environ.get("GO2_SEARCH_SESSION_ID") != map_session:
            raise ValueError("search_scope_session_mismatch")
        return {"bounds": list(bounds), "mode": "bounded", "session_id": map_session,
                "source": "environment"}
    elif not user_search:
        raise ValueError("search_scope_required")
    return {"bounds": None, "mode": "reachable", "session_id": map_session,
            "source": "user_task"}


def describe_search_result(result: dict) -> dict:
    """Keep sightings, arrival, incomplete coverage and stop proof distinct."""
    result = dict(result)
    if result.get("mode", "find_object") != "find_object":
        return result
    reason = result.get("reason") or result.get("error") or "unknown"
    seen = bool(result.get("found") or result.get("target_hint")
                or (result.get("evidence") or {}).get("found"))
    observed = bool((result.get("coverage") or {}).get("observations")
                    or result.get("evidence") or result.get("steps"))
    result["found"] = seen
    if reason == "stop_unconfirmed":
        outcome, message = "stop_unconfirmed", "搜索已中止，但还没有确认机身停稳；不能继续移动，也不能算找到并接近了目标。"
    elif result.get("result") == "ok":
        outcome, message = "nearby", "连续画面判断目标已在附近，机身已确认停稳；尚无精确目标坐标或距离。"
    else:
        outcome = "seen_not_reached" if seen else "not_seen" if observed else "not_started"
        opening = ("画面中出现过疑似目标，但还没确认已到附近。" if seen else
                   "已检查部分画面，暂未识别到目标，不能据此判断室内没有。" if observed else
                   "搜索还没有开始。")
        reasons = {
            "search_scope_required": "搜索范围配置尚未就绪。",
            "search_scope_session_mismatch": "已有搜索边界属于上一次定位，需要重新设置当前会话的边界。",
            "map_session_unavailable": "当前定位会话尚未就绪。",
            "map_session_changed": "定位会话已改变，本次搜索已结束。",
            "localization_unavailable": "定位数据暂不可用。",
            "motion_not_observed": "发出移动指令后，没有观察到足够的实际位移或转动。",
            "no_known_free_observation_path": "当前没有可通行的目标接近路径。",
            "coverage_exhausted": "当前没有新的可达观察点，遮挡区域仍未检查。",
            "time_budget_exhausted": "本次搜索时间已用尽。",
            "step_budget_exhausted": "本次搜索步数已用尽。",
            "distance_budget_exhausted": "本次搜索行程已用尽。",
            "control_lease_changed": "控制权已交给其他操作。",
            "cancelled": "本次搜索已取消。",
            "search_step_failed": "搜索处理发生错误。",
        }
        message = opening + reasons.get(reason, f"本次搜索已结束，原因：{reason}。")
    result.update(outcome=outcome, message=message)
    return result


class SearchState(TypedDict, total=False):
    task_id: str
    mode: str
    target: str
    map_session: str
    phase: str
    steps: int
    distance_m: float
    deadline: float
    observation: dict
    candidate: dict
    coverage: dict
    receipt: dict
    result: dict
    misses: int
    failures: int
    target_hint: dict | None
    target_views: list[dict]
    approach_attempts: int
    scope: dict
    last_detection: dict


def build_search_graph(adapter, *, checkpointer=None):
    """Inject device operations so the exact production graph can be replayed."""
    def terminal(state, reason, status="blocked", **extra):
        detection = state.get("last_detection") or {}
        return {"phase": "finished", "result": {"result": status, "reason": reason,
                "target": state.get("target", ""), "task_id": state["task_id"],
                "coverage": state.get("coverage", {}), "steps": state.get("steps", 0),
                "distance_m": state.get("distance_m", 0.0),
                "target_hint": state.get('target_hint'), "found": bool(detection),
                "evidence": detection, **extra}}

    async def guard(state):
        reason = await adapter.guard(state)
        if reason:
            return terminal(state, reason)
        if time.time() >= state["deadline"]:
            return terminal(state, "time_budget_exhausted", "timeout")
        if state.get("steps", 0) >= SEARCH_MAX_STEPS:
            return terminal(state, "step_budget_exhausted")
        if state.get("distance_m", 0) >= SEARCH_MAX_DISTANCE_M:
            return terminal(state, "distance_budget_exhausted")
        return None

    async def observe(state):
        blocked = await guard(state)
        if blocked:
            return blocked
        observation = await adapter.observe(state)
        if observation.get("reason"):
            return terminal(state, observation["reason"])
        summary = await adapter.record(observation)
        if summary.get("reason"):
            return terminal(state, summary["reason"])
        found = observation.get("found") is True and state["mode"] == "find_object"
        await adapter.progress("search_candidate" if found else "search_observe", state,
                               observation=observation, coverage=summary)
        return {"observation": observation, "coverage": summary,
                "last_detection": observation if found else state.get("last_detection", {}),
                "target_hint": observation.get("target_hint") or state.get("target_hint"),
                # A single detection is sufficient to pursue a direction.
                # Recheck only when the image says we are already nearby,
                # immediately before reporting completion.
                "phase": "confirm" if found and observation.get('observation', {}).get(
                    'proximity') == 'nearby' else "select"}

    async def confirm(state):
        blocked = await guard(state)
        if blocked:
            return blocked
        confirmed = await adapter.observe(state)
        if confirmed.get("reason"):
            return terminal(state, confirmed["reason"], found=True,
                            evidence=state.get("observation", {}))
        first = state["observation"]
        if confirmed.get("frame_id") == first.get("frame_id"):
            return terminal(state, "confirmation_frame_reused", found=True, evidence=first)
        if not confirmed.get("found"):
            return {"observation": confirmed, "phase": "select", "misses": state.get("misses", 0) + 1}
        await adapter.progress("search_confirmed", state, observation=confirmed)
        confirmed = dict(confirmed, nearby_confirmed=(
            first.get('observation', {}).get('proximity') == 'nearby'
            and confirmed.get('observation', {}).get('proximity') == 'nearby'))
        return {"observation": confirmed, "last_detection": confirmed, "phase": "approach",
                "target_hint": confirmed.get("target_hint") or state.get("target_hint")}

    async def select(state):
        blocked = await guard(state)
        if blocked:
            return blocked
        selected = await adapter.select(state)
        if not selected.get("target"):
            reason = selected.get("reason", "no_reachable_observation")
            # Even exhausted coverage does not establish absence behind occluders.
            return terminal(state, reason, "ok" if state["mode"] == "explore" and
                            reason == "coverage_exhausted" else "blocked",
                            found=bool(state.get('last_detection') or state.get('target_hint')),
                            evidence=state.get('last_detection') or state.get('observation', {}))
        return {"candidate": selected["target"], "phase": "navigate"}

    async def navigate(state):
        blocked = await guard(state)
        if blocked:
            return blocked
        distance = state.get("distance_m", 0.0)
        proposed = state["candidate"].get("path_cost_m", 0)
        if state["mode"] == "find_object":
            proposed = min(proposed, OBSERVATION_SEGMENT_M)
        if distance + proposed > SEARCH_MAX_DISTANCE_M:
            return terminal(state, "distance_budget_exhausted")
        await adapter.progress("search_navigate", state)
        receipt = await adapter.navigate(state)
        distance += max(0.0, float(receipt.get("distance_m", 0)))
        if receipt.get("result") != "ok":
            # A brief telemetry gap stops the segment immediately. Once the
            # stop receipt proves fresh same-session samples, observe again
            # before choosing another move instead of ending the whole search.
            pose = receipt.get('last_trusted_pose') or {}
            if (receipt.get('reason') == 'localization_unavailable'
                    and receipt.get('stop', {}).get('result') == 'ok'
                    and pose.get('session_id') == state.get('map_session')
                    and state.get('failures', 0) < 2):
                return {'receipt': receipt, 'phase': 'observe',
                        'steps': state.get('steps', 0) + 1,
                        'failures': state.get('failures', 0) + 1, 'distance_m': distance}
            # Only a geometrically blocked goal is eligible for a bounded
            # alternate view. Lost authority/telemetry never triggers retry.
            retryable = receipt.get("reason") in {"path_not_found", "goal_blocked", "no_path", "no_known_free_path"}
            targeted = state['candidate'].get('source') == 'target_reobservation'
            if targeted and receipt.get('reason') in {'body_clearance_unavailable', 'turn_clearance_unavailable'}:
                retryable = True
            if retryable and state.get("failures", 0) < 2:
                await adapter.mark_failed(state["candidate"])
                return {"receipt": receipt, "phase": "select",
                        "steps": state.get("steps", 0) + 1,
                        "target_views": [*state.get('target_views', []), state['candidate']]
                                        if targeted else state.get('target_views', []),
                        "failures": state.get("failures", 0) + 1,
                        "distance_m": distance}
            return terminal(state, receipt.get("reason") or "navigation_unconfirmed",
                            receipt.get("result", "blocked"), navigation=receipt)
        views = state.get("target_views", [])
        if state["candidate"].get("source") == "target_reobservation":
            views = [*views, state["candidate"]]
        return {"receipt": receipt, "steps": state.get("steps", 0) + 1,
                "target_views": views, "distance_m": distance, "phase": "observe"}

    async def approach(state):
        blocked = await guard(state)
        if blocked:
            return blocked
        await adapter.progress("search_approach", state)
        receipt = await adapter.approach(state)
        status = receipt.get("result", "blocked")
        reason = receipt.get("reason") or ("arrival_verified" if status == "ok" else "approach_unconfirmed")
        hint = receipt.get("target_hint") or state.get("target_hint")
        # Approximate object search follows direction using the existing graph.
        # It does not wait for metric localization or exhaust a separate depth
        # retry budget. The task's time, travel and step budgets still apply.
        if status == 'blocked' and reason in {'target_view_needed', 'target_nearby_recheck'} and hint:
            return {'receipt': receipt, 'target_hint': hint,
                    'phase': 'observe' if reason == 'target_nearby_recheck' else 'select'}
        # Losing one view or failing depth is a request for another view, not
        # evidence that the object vanished. Authorization/stop faults stay terminal.
        recoverable = reason in {"target_not_visible", "object_returns_unavailable",
            "target_lidar_disagreement", "target_confirmation_mismatch",
            "arrival_target_mismatch", "no_known_free_observation_path"}
        attempts = state.get("approach_attempts", 0) + 1
        if (status == "blocked" and recoverable and hint and attempts < 3
                and len(state.get("target_views", [])) < TARGET_VIEW_ATTEMPTS):
            return {"receipt": receipt, "target_hint": hint,
                    "approach_attempts": attempts, "phase": "select"}
        result = terminal(state, reason, status, found=True,
                          evidence=state.get("observation", {}), approach=receipt)
        return result

    async def next_phase(state):
        return state["phase"]

    graph = StateGraph(SearchState)
    for name, node in (("observe", observe), ("confirm", confirm), ("select", select),
                       ("navigate", navigate), ("approach", approach)):
        graph.add_node(name, node)
        graph.add_conditional_edges(name, next_phase,
                                    {phase: phase for phase in ("observe", "confirm", "select", "navigate", "approach")} | {"finished": END})
    graph.add_edge(START, "observe")
    return graph.compile(checkpointer=checkpointer)


class DeviceSearch:
    def __init__(self, context, task_id, bounds, map_session):
        from cloud.go2.navigation.observation_coverage import ObservationCoverage
        from cloud.agents.common.effects import FileEffectLedger
        self.context, self.task_id, self.bounds = context, task_id, bounds
        self.reachable_scope = bounds is None
        self.map_session = map_session
        self.coverage = ObservationCoverage(map_session, bounds=bounds)
        self.ledger = FileEffectLedger()
        self.after_frame = None
        self.after_time = time.monotonic()
        self.last_pose = None
        self.last_pose_time = None
        self.travelled_m = 0.0
        self.progress_sequence = 0

    def refresh_scope(self, snapshot=None):
        """Follow current map extent within this task's reachable scope.

        Extent is a calculation bound, not evidence that its cells are walkable:
        Selector and Navigator still check obstacles and body clearance.
        """
        if not self.reachable_scope:
            return None
        if snapshot is None:
            from cloud.go2.navigation.navigator import navigator
            snapshot, reason = navigator.snapshot()
            if snapshot is None:
                return reason
        pose, grid = snapshot
        if (getattr(grid, "map_session", None) != self.map_session
                or pose.get("session_id") != self.map_session):
            return "map_session_changed"
        xmin, ymin = (float(value) for value in grid.origin[:2])
        self.bounds = (xmin, ymin, xmin + int(grid.width[0]) * float(grid.resolution),
                       ymin + int(grid.width[1]) * float(grid.resolution))
        self.coverage.bounds = self.bounds
        return None

    async def guard(self, state):
        from cloud.go2.connection import go2
        from cloud.go2.control import control_lease
        from cloud.go2.state import runtime_for
        from cloud.go2.tools.executor import validate_execution_context
        try:
            validate_execution_context(self.context)
        except ValueError as exc:
            return str(exc)
        if not go2.is_connected:
            return "stop_unconfirmed"
        if getattr(go2, "_odom_session_id", None) != self.map_session:
            return "map_session_changed"
        runtime = runtime_for(go2)
        if runtime and runtime.active_reason():
            return runtime.active_reason()
        lease = control_lease.snapshot()
        if lease is None or lease.owner_session_id != self.context.session_id:
            return "control_lease_changed"
        control_lease.heartbeat(self.context)
        # Initialize the calculation extent once. Refresh it from the fresh
        # snapshot already required by record/select, not from this 5 Hz
        # ownership monitor: Navigator and visual capture own telemetry gaps.
        if self.bounds is None:
            scope_error = self.refresh_scope()
            if scope_error:
                return scope_error
        pose = go2.odom
        if not pose or not (self.bounds[0] <= pose.get("x", math.inf) <= self.bounds[2]
                            and self.bounds[1] <= pose.get("y", math.inf) <= self.bounds[3]):
            return "outside_search_scope"
        stamp = getattr(go2, "odom_received_at", None)
        if stamp is not None and stamp != self.last_pose_time and time.monotonic() - stamp <= 1.0:
            if self.last_pose is not None:
                self.travelled_m += math.hypot(pose["x"] - self.last_pose["x"], pose["y"] - self.last_pose["y"])
            self.last_pose, self.last_pose_time = pose.copy(), stamp
        if self.travelled_m >= SEARCH_MAX_DISTANCE_M:
            return "distance_budget_exhausted"
        return None

    async def observe(self, state):
        from cloud.go2.perception.visual_target import observe_target, TargetUnlocalized
        from cloud.go2.navigation.target_view import make_target_hint
        from cloud.go2.connection import go2
        if getattr(go2, '_velocity_command_active', False):
            return {"reason": "observation_requires_stop"}
        for attempt in range(2):
            try:
                reason = await self.guard(state)
                if reason:
                    return {"reason": reason}
                sample = await asyncio.wait_for(observe_target(
                    state["target"] if state["mode"] == "find_object" else "",
                    after_frame_id=self.after_frame, after_received_at=self.after_time),
                    timeout=min(OBSERVATION_TIMEOUT_S, max(0.01, state["deadline"] - time.time())))
                reason = await self.guard(state)
                if reason:
                    return {"reason": reason}
                frame = sample["frame"]
                self.after_frame = frame["frame_id"]
                self.after_time = frame["received_at"]
                return {"found": bool(sample.get("found")), "frame_id": frame["frame_id"],
                        "image_time": frame["received_at"], "pose": sample["pose"],
                        "target_hint": make_target_hint(sample),
                        "quality": sample.get("quality", 0.0),
                        "media_ref": sample.get("media_ref") or sample.get("image_url"),
                        "observation": sample.get("observation", {})}
            except TargetUnlocalized as exc:
                if attempt == 0 and exc.reason in {'pose_changed_during_detection',
                                                   'fresh_still_frame_unavailable'}:
                    # Discard the failed frame completely, including coverage.
                    self.after_time = time.monotonic()
                    await asyncio.sleep(.25)
                    continue
                return {"reason": exc.reason}
            except asyncio.TimeoutError:
                return {"reason": "observation_timeout"}

    async def record(self, observation):
        from cloud.go2.navigation.navigator import navigator
        snapshot, reason = navigator.snapshot()
        if snapshot is None:
            return {"reason": reason, "unchecked": True}
        scope_error = self.refresh_scope(snapshot)
        if scope_error:
            return {"reason": scope_error, "unchecked": True}
        recorded = await invoke_blocking(self.coverage.record, snapshot[1], observation["pose"],
                                observation["image_time"], observation["quality"],
                                target_result=observation.get("found"))
        if not recorded:
            return {"reason": "observation_evidence_insufficient", "unchecked": True}
        return await invoke_blocking(self.coverage.summary, snapshot[1])

    async def select(self, state):
        from cloud.go2.navigation.navigator import navigator
        from cloud.go2.navigation.frontier import select_observation_target
        snapshot, reason = navigator.snapshot()
        if snapshot is None:
            return {"reason": reason}
        scope_error = self.refresh_scope(snapshot)
        if scope_error:
            return {"reason": scope_error}
        hint = state.get('target_hint')
        views = state.get('target_views', [])
        if hint:
            from cloud.go2.navigation.target_view import select_target_view, target_hint_valid
            point = await invoke_blocking(select_target_view, snapshot[1], snapshot[0],
                                           hint, bounds=self.bounds, attempted=views, approach=True)
            if point is not None:
                point = dict(point, path_cost_m=math.hypot(
                    point['x']-snapshot[0]['x'], point['y']-snapshot[0]['y']),
                    map_session=self.map_session)
                return {'target': point, 'reason': 'target_view_selected'}
            if target_hint_valid(hint, snapshot[0]):
                return {'reason': 'no_known_free_observation_path'}
        selected = await invoke_blocking(select_observation_target, snapshot[1], snapshot[0],
                                           self.coverage, bounds=self.bounds)
        candidate = selected.target.as_dict() if selected.target else None
        if candidate is not None:
            # Real SessionMap coordinates originate in NumPy arrays. The
            # checkpoint serializer accepts native scalars, not numpy.float64.
            for key in ("x", "y", "heading", "score", "path_cost_m"):
                candidate[key] = float(candidate[key])
        return {"target": candidate,
                "reason": selected.reason}

    async def mark_failed(self, candidate):
        from cloud.go2.navigation.frontier import ObservationTarget
        if candidate.get('source') != 'target_reobservation':
            self.coverage.mark_failed(ObservationTarget(**candidate))

    async def effect(self, state, name, params, invoke):
        step = f"search:{state.get('steps', 0)}:{name}"
        identity = ("go2", self.context.session_id, self.task_id, step)
        record = await invoke_blocking(self.ledger.reserve, *identity, name, params)
        if record["reservation"] != "new":
            return {"result": "blocked", "reason": "prior_dispatch_unconfirmed",
                    "execution_id": step, "previous_receipt": record.get("result")}
        reason = await self.guard(state)
        if reason:
            result = {"result": "blocked", "reason": reason}
        else:
            result = await invoke()
        result["execution_id"] = step
        await invoke_blocking(self.ledger.mark_result, *identity, result)
        return result

    async def navigate(self, state):
        from cloud.go2.navigation.navigator import navigator
        from cloud.go2.connection import go2
        candidate = state["candidate"]
        async def invoke():
            start = go2.odom.copy()
            reposition = candidate.get('source') in {'observation_reposition', 'target_reobservation'}
            segmented = state.get('mode') == 'find_object'
            options = ({'minimum_command_speed': .25, 'speed_limit': .30}
                       if reposition or segmented else {})
            if reposition:
                options.update(arrival_tolerance_m=.08,
                               holonomic=bool(candidate.get('holonomic')))
            navigation = asyncio.create_task(navigator.go_to_coordinate(
                candidate["x"], candidate["y"], heading=candidate["heading"],
                owner_session_id=self.context.session_id, bounds=self.bounds, **options))
            # No vision inference runs alongside movement. Odom limits each
            # leg; the existing Graph observes only after confirmed stopping.
            watcher = (asyncio.create_task(self._watch_observation_segment(state, navigation, start))
                       if segmented else None)
            children = [navigation] + ([watcher] if watcher is not None else [])
            try:
                async with asyncio.timeout(min(65.0, max(0.01, state["deadline"] - time.time()))):
                    if watcher is None:
                        result = await navigation
                    else:
                        done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
                        if navigation in done:
                            result = navigation.result()
                        else:
                            segment = watcher.result()
                            if segment is None:
                                result = await navigation
                            else:
                                stop = await navigator.stop_and_confirm(owner_session_id=self.context.session_id)
                                if not navigation.done():
                                    navigation.cancel()
                                await asyncio.gather(navigation, return_exceptions=True)
                                reason = segment.get('reason')
                                stopped = stop.get('result') == 'ok'
                                result = {'status': 'succeeded' if stopped and not reason else 'blocked',
                                    'reason': ('observation_segment_stopped' if stopped and not reason
                                               else reason if stopped else 'stop_unconfirmed'),
                                    'stop': stop, 'goal_reached': False,
                                    'planned_observation_goal': candidate,
                                    'last_trusted_pose': navigator.last_trusted_pose or go2.odom.copy(),
                                    'segment': segment}
            finally:
                for child in children:
                    if not child.done():
                        child.cancel()
                await asyncio.gather(*children, return_exceptions=True)
            status = result.get('status')
            self.after_time = time.monotonic()
            end = result.get('last_trusted_pose') or start
            displacement = math.hypot(end['x']-start['x'], end['y']-start['y'])
            rotation = abs(math.atan2(math.sin(end.get('heading', 0)-start.get('heading', 0)),
                                      math.cos(end.get('heading', 0)-start.get('heading', 0))))
            enough_motion = rotation >= .04 if candidate.get('view_kind') == 'turn' else displacement >= .10
            if (reposition and status == 'succeeded'
                    and result.get('reason') != 'observation_segment_stopped'
                    and (end.get('session_id') != start.get('session_id')
                         or not start.get('session_id') or not math.isfinite(displacement)
                         or not enough_motion)):
                result = {'status': 'blocked', 'reason': 'observation_move_unconfirmed',
                          'last_trusted_pose': end, 'navigation_receipt': result}
                status = 'blocked'
            return {**result, 'result': 'ok' if status == 'succeeded' else
                    'error' if status == 'failed' else status,
                    'distance_m': max(displacement, (result.get('segment') or {}).get('distance_m', 0.))}
        return await self.effect(state, 'navigate', candidate, invoke)

    async def _watch_observation_segment(self, state, navigation, start):
        from cloud.go2.connection import go2
        from cloud.go2.navigation.navigator import POSE_STALE_S
        begun = time.monotonic()
        previous, distance, rotation = start, 0., 0.
        while not navigation.done():
            reason = await self.guard(state)
            if reason:
                return {'reason': reason, 'distance_m': distance}
            # Navigator alone handles map updates/brief map gaps. This watcher
            # measures movement, and must not add a second map freshness gate.
            stamp = getattr(go2, 'odom_received_at', None)
            pose = go2.odom
            if (type(stamp) not in (int, float) or not 0 <= time.monotonic()-stamp <= POSE_STALE_S
                    or getattr(go2, 'odom_frame_id', None) != 'odom'
                    or not pose or not all(type(pose.get(k)) in (int, float) and math.isfinite(pose[k])
                                           for k in ('x', 'y', 'heading'))):
                return {'reason': 'localization_unavailable', 'distance_m': distance}
            if pose.get('session_id') != start.get('session_id'):
                return {'reason': 'map_session_changed', 'distance_m': distance}
            distance += math.hypot(pose['x']-previous['x'], pose['y']-previous['y'])
            rotation += abs(math.atan2(math.sin(pose['heading']-previous['heading']),
                                      math.cos(pose['heading']-previous['heading'])))
            previous = pose.copy()
            if (distance >= OBSERVATION_SEGMENT_M or rotation >= OBSERVATION_SEGMENT_RAD
                    or time.monotonic()-begun >= OBSERVATION_SEGMENT_S):
                net_distance = math.hypot(pose['x']-start['x'], pose['y']-start['y'])
                net_rotation = abs(math.atan2(math.sin(pose['heading']-start['heading']),
                                             math.cos(pose['heading']-start['heading'])))
                return {'distance_m': distance, 'rotation_rad': rotation,
                        'reason': None if net_distance >= .02 or net_rotation >= .04 else 'motion_not_observed'}
            await asyncio.sleep(.05)
        return None

    async def approach(self, state):
        from cloud.go2.navigation.navigator import navigator
        observation = state.get('observation', {})
        evidence = observation.get('observation', {})
        hint = state.get('target_hint')
        reason = await self.guard(state)
        if reason:
            return {'result': 'blocked', 'reason': reason}
        if not observation.get('found') or not observation.get('nearby_confirmed'):
            return {'result': 'blocked', 'target_hint': hint,
                    'reason': 'target_nearby_recheck' if observation.get('found') and
                    evidence.get('proximity') == 'nearby' else 'target_view_needed'}
        async def invoke():
            stop = await navigator.stop_and_confirm(owner_session_id=self.context.session_id)
            reason = await self.guard(state)
            if stop.get('result') != 'ok' or reason:
                return {'result': 'blocked', 'reason': reason or 'stop_unconfirmed', 'stop': stop}
            return {'result': 'ok', 'reason': 'target_nearby_visual',
                    'confirmation': 'visual_nearby', 'precision': 'approximate',
                    'target': state['target'], 'target_hint': hint,
                    'target_location': None, 'distance_to_target_m': None,
                    'last_trusted_pose': observation.get('pose'), 'stop': stop,
                    'evidence': observation,
                    'message': '已找到目标，连续画面判断已在附近并已停稳；未测得精确目标坐标或距离。'}
        return await self.effect(state, "approach", {"target": state["target"],
                                 "precision": "approximate"}, invoke)

    async def progress(self, stage, state, **fields):
        from cloud.api.intents import publish_agent_event
        from cloud.observability import current_event_context
        correlation = current_event_context()
        self.progress_sequence += 1
        observation = fields.get("observation") or state.get("observation") or {}
        autonomous = self.context.origin_kind == "agent_autonomous"
        publish_agent_event("agent_progress", agent_id="go2",
            session_id="" if autonomous else self.context.session_id,
            scope="space" if autonomous else "session", space_id=self.context.space_id,
            task_id=self.task_id, run_id=self.task_id, stage=stage,
            trace_id=correlation.get("trace_id"), parent_run_id=correlation.get("parent_run_id"),
            sequence=self.progress_sequence,
            status=(state.get("result") or {}).get("result", "executing"),
            target=state.get("target"), mode=state.get("mode"), coverage=fields.get("coverage", state.get("coverage", {})),
            reason=(state.get("result") or {}).get("reason"),
            media_ref=observation.get("media_ref"), source="object_search")


async def _run_search(target: str = "", *, mode: str = "find_object", context=None,
                     task_id: str | None = None, deadline: float | None = None) -> dict:
    from cloud.agents.common.checkpoint import FileCheckpointSaver
    from cloud.go2.execution import current_context, use_context
    from cloud.go2.connection import go2
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.control import control_lease, session_tasks
    from cloud.go2.state import runtime_for
    from cloud.go2.tools.executor import validate_execution_context
    context = context or current_context()
    requested = _request.get()
    task_id = task_id or (requested[0] if requested else "search_" + uuid.uuid4().hex)
    deadline = min(deadline or (requested[1] if requested else time.time() + SEARCH_BUDGET_S),
                   time.time() + (60.0 if mode == "explore" else SEARCH_BUDGET_S),
                   context.expires_at or math.inf)
    try:
        validate_execution_context(context)
    except ValueError as exc:
        return {"result": "blocked", "reason": str(exc), "task_id": task_id}
    if mode not in {"explore", "find_object"} or (mode == "find_object" and not target.strip()):
        return {"result": "rejected", "reason": "invalid_search_request"}
    checkpointer = FileCheckpointSaver()
    config = {"configurable": {"thread_id": f"go2:search:{context.session_id}:{task_id}"},
              "recursion_limit": SEARCH_MAX_STEPS * 5 + 20}
    # Historical results survive reconnects. Reading one never reacquires the
    # robot or binds its old coordinates to the new localization session.
    existing = await checkpointer.aget_tuple(config)
    if existing is not None:
        values = existing.checkpoint.get("channel_values", {})
        return values.get("result") or {"result": "blocked", "reason": "search_recovery_requires_new_observation",
                                        "task_id": task_id}
    session = getattr(go2, "_odom_session_id", None)
    if not session:
        return {"result": "blocked", "reason": "map_session_unavailable"}
    try:
        scope = resolve_search_scope(context, mode, session)
    except ValueError as exc:
        return {"result": "blocked", "reason": str(exc), "task_id": task_id}
    bounds = tuple(scope["bounds"]) if scope["bounds"] is not None else None
    adapter = DeviceSearch(context, task_id, bounds, session)
    if mode == "explore":
        # Segments share bounded coverage until scope or localization changes.
        global _exploration_coverage
        coverage_key = (context.session_id, session, bounds)
        if _exploration_coverage is not None and _exploration_coverage[0] == coverage_key:
            adapter.coverage = _exploration_coverage[1]
        else:
            _exploration_coverage = (coverage_key, adapter.coverage)
    control_lease.acquire(context)
    runtime = runtime_for(go2)
    token = None
    if runtime and runtime.data["life_state"] == "idle":
        token = runtime.begin_life("explore" if mode == "explore" else "play", context.session_id, task_id)
    session_tasks.track(context.session_id, asyncio.current_task())
    state = {"task_id": task_id, "mode": mode, "target": target,
             "map_session": session, "phase": "observe", "steps": 0, "distance_m": 0.0,
             "deadline": deadline, "coverage": {}, "scope": scope}
    async def monitor():
        while True:
            reason = await adapter.guard(state)
            if reason:
                return reason
            await asyncio.sleep(0.2)

    graph_task = monitor_task = None
    result = None
    graph = build_search_graph(adapter, checkpointer=checkpointer)

    async def stop_work():
        if graph_task and not graph_task.done():
            graph_task.cancel()
        if graph_task is not None:
            await asyncio.gather(graph_task, return_exceptions=True)
        try:
            return await asyncio.wait_for(navigator.stop_and_confirm(
                owner_session_id=context.session_id), timeout=5.0)
        except Exception:
            return {"result": "accepted", "reason": "stop_unconfirmed"}

    async def finish(receipt):
        nonlocal result
        stopped = await stop_work()
        # A failed node never returns the graph's accumulated state to the
        # caller. Preserve completed observation evidence from its checkpoint.
        try:
            latest = await graph.aget_state(config)
            state.update({key: value for key, value in latest.values.items()
                          if key in SearchState.__annotations__})
        except Exception:
            logging.getLogger(__name__).warning("search checkpoint recovery unavailable task=%s", task_id)
        if stopped.get("result") != "ok":
            receipt = {**receipt, "result": "accepted", "cause": receipt.get("reason"),
                       "reason": "stop_unconfirmed", "stop": stopped}
        else:
            receipt = {**receipt, "stop": stopped}
        result = {**receipt, "task_id": task_id, "mode": mode,
                  "distance_m": round(adapter.travelled_m, 3)}
        result.setdefault("coverage", state.get("coverage", {}))
        result.setdefault("steps", state.get("steps", 0))
        result.setdefault("scope", scope)
        result.setdefault("target", target)
        result["found"] = bool(result.get("found") or state.get("last_detection"))
        if state.get("last_detection"):
            result["evidence"] = state["last_detection"]
        result = describe_search_result(result)
        if state.get("receipt"):
            result.setdefault("navigation", state["receipt"])
        if result.get("result") in {"accepted", "executing", "unknown"} and runtime:
            # No automatic exploration after loss of physical stop proof.
            runtime.exploration_enabled = False
        state.update(phase="finished", result=result)
        try:
            await graph.aupdate_state(config, {"phase": "finished", "result": result}, as_node="observe")
            await adapter.progress("search_finished", state)
        except Exception:
            # Physical stopping precedes observability; a failed checkpoint
            # must not cause another attempt at the motion itself.
            logging.getLogger(__name__).exception("search terminal persistence failed")
        return result

    try:
        with use_context(context):
            graph_task = asyncio.create_task(graph.ainvoke(state, config, durability="sync"))
            monitor_task = asyncio.create_task(monitor())
            done, _ = await asyncio.wait({graph_task, monitor_task},
                                        timeout=max(0.01, deadline - time.time()),
                                        return_when=asyncio.FIRST_COMPLETED)
            if monitor_task in done:
                return await finish({"result": "blocked", "reason": monitor_task.result()})
            if graph_task not in done:
                raise asyncio.TimeoutError
            state = graph_task.result()
            return await finish(state["result"])
    except asyncio.CancelledError:
        await finish({"result": "cancelled", "reason": "cancelled"})
        raise
    except asyncio.TimeoutError:
        return await finish({"result": "timeout", "reason": "time_budget_exhausted"})
    except Exception as exc:
        # Exception text may include model input or credentials from adapters.
        # Type and source locations are sufficient to locate the failing code.
        logging.getLogger(__name__).error("search step failed task=%s error=%s frames=%s",
            task_id, type(exc).__name__,
            [(frame.name, frame.lineno) for frame in traceback.extract_tb(exc.__traceback__)])
        return await finish({"result": "error", "reason": "search_step_failed",
                             "error_type": type(exc).__name__})
    finally:
        pending = [task for task in (graph_task, monitor_task) if task is not None]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if token:
            runtime.finish_life(token)
        if result is not None and result.get("result") not in {"accepted", "executing", "unknown"}:
            control_lease.release(context.session_id)


async def run_search(target: str = "", *, mode: str = "find_object", context=None,
                     task_id: str | None = None, deadline: float | None = None) -> dict:
    """Serialize duplicate tasks before checking their existing effects."""
    from cloud.agents.common.checkpoint import FileCheckpointSaver
    from cloud.go2.execution import current_context
    context = context or current_context()
    requested = _request.get()
    task_id = task_id or (requested[0] if requested else "search_" + uuid.uuid4().hex)
    thread_id = f"go2:search:{context.session_id}:{task_id}"
    async with FileCheckpointSaver().async_run_lock(thread_id):
        result = await _run_search(target, mode=mode, context=context,
                                   task_id=task_id, deadline=deadline)
        return describe_search_result({"target": target, "mode": mode, "task_id": task_id, **result})
