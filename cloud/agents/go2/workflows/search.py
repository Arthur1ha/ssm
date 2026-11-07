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


def build_search_graph(adapter, *, checkpointer=None):
    """Inject device operations so the exact production graph can be replayed."""
    def terminal(state, reason, status="blocked", **extra):
        return {"phase": "finished", "result": {"result": status, "reason": reason,
                "target": state.get("target", ""), "task_id": state["task_id"],
                "coverage": state.get("coverage", {}), "steps": state.get("steps", 0),
                "distance_m": state.get("distance_m", 0.0), **extra}}

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
                "phase": "confirm" if found else "select"}

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
        return {"observation": confirmed, "phase": "approach"}

    async def select(state):
        blocked = await guard(state)
        if blocked:
            return blocked
        selected = await adapter.select(state)
        if not selected.get("target"):
            reason = selected.get("reason", "no_reachable_observation")
            # Even exhausted coverage does not establish absence behind occluders.
            return terminal(state, reason, "ok" if state["mode"] == "explore" and
                            reason == "coverage_exhausted" else "blocked", found=False)
        return {"candidate": selected["target"], "phase": "navigate"}

    async def navigate(state):
        blocked = await guard(state)
        if blocked:
            return blocked
        distance = state.get("distance_m", 0.0)
        if distance + state["candidate"].get("path_cost_m", 0) > SEARCH_MAX_DISTANCE_M:
            return terminal(state, "distance_budget_exhausted")
        await adapter.progress("search_navigate", state)
        receipt = await adapter.navigate(state)
        distance += max(0.0, float(receipt.get("distance_m", 0)))
        if receipt.get("result") != "ok":
            # Only a geometrically blocked goal is eligible for a bounded
            # alternate view. Lost authority/telemetry never triggers retry.
            retryable = receipt.get("reason") in {"path_not_found", "goal_blocked", "no_path", "no_known_free_path"}
            if retryable and state.get("failures", 0) < 2:
                await adapter.mark_failed(state["candidate"])
                return {"receipt": receipt, "phase": "select",
                        "steps": state.get("steps", 0) + 1,
                        "failures": state.get("failures", 0) + 1,
                        "distance_m": distance}
            return terminal(state, receipt.get("reason") or "navigation_unconfirmed",
                            receipt.get("result", "blocked"), navigation=receipt)
        return {"receipt": receipt, "steps": state.get("steps", 0) + 1,
                "distance_m": distance, "phase": "observe"}

    async def approach(state):
        blocked = await guard(state)
        if blocked:
            return blocked
        await adapter.progress("search_approach", state)
        receipt = await adapter.approach(state)
        status = receipt.get("result", "blocked")
        reason = receipt.get("reason") or ("arrival_verified" if status == "ok" else "approach_unconfirmed")
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
        """Follow current map extent only after explicit reachable-space authorization.

        Extent is a calculation bound, not evidence that its cells are walkable:
        selector and Navigator still require measured free paths and body clearance.
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
        try:
            sample = await asyncio.wait_for(observe_target(
                state["target"] if state["mode"] == "find_object" else "",
                after_frame_id=self.after_frame, after_received_at=self.after_time),
                timeout=min(OBSERVATION_TIMEOUT_S, max(0.01, state["deadline"] - time.time())))
            frame = sample["frame"]
            self.after_frame = frame["frame_id"]
            self.after_time = frame["received_at"]
            return {"found": bool(sample.get("found")), "frame_id": frame["frame_id"],
                    "image_time": frame["received_at"], "pose": sample["pose"],
                    "quality": sample.get("quality", 0.0),
                    "media_ref": sample.get("media_ref") or sample.get("image_url"),
                    "observation": sample.get("observation", {})}
        except TargetUnlocalized as exc:
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
        start = go2.odom.copy()
        async def invoke():
            navigation = asyncio.create_task(navigator.go_to_coordinate(
                candidate["x"], candidate["y"], heading=candidate["heading"],
                owner_session_id=self.context.session_id, bounds=self.bounds))
            # This observer belongs to the same effect and HTTP task. It can
            # request a stop, never dispatch motion or establish target arrival.
            observer = (asyncio.create_task(self._observe_transit(state, navigation))
                        if state.get("mode") == "find_object" else None)
            children = [navigation] + ([observer] if observer is not None else [])
            try:
                async with asyncio.timeout(min(65.0, max(0.01, state["deadline"] - time.time()))):
                    if observer is None:
                        result = await navigation
                    else:
                        done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
                        if navigation in done:
                            result = navigation.result()
                        else:
                            observation = observer.result()
                            if observation is None:
                                result = await navigation
                            else:
                                stop = await navigator.stop_and_confirm(
                                    owner_session_id=self.context.session_id)
                                # Navigator normally cancels its owned task;
                                # explicitly join it before returning to Graph.
                                if not navigation.done():
                                    navigation.cancel()
                                await asyncio.gather(navigation, return_exceptions=True)
                                stopped = stop.get("result") == "ok"
                                reason = observation.get("reason")
                                result = {"status": "succeeded" if stopped and not reason else "blocked",
                                    "reason": ("transit_candidate_stopped" if stopped and not reason
                                               else reason if stopped else "stop_unconfirmed"),
                                    "stop": stop, "last_trusted_pose": go2.odom,
                                    "transit_candidate": observation if not reason else None}
            finally:
                for child in children:
                    if not child.done():
                        child.cancel()
                await asyncio.gather(*children, return_exceptions=True)
            status = result.get("status")
            self.after_time = time.monotonic()
            end = result.get("last_trusted_pose") or start
            return {**result, "result": "ok" if status == "succeeded" else
                    "error" if status == "failed" else status,
                    "distance_m": (result.get("diagnostics") or {}).get("path_length_m",
                        math.hypot(end["x"] - start["x"], end["y"] - start["y"]))}
        return await self.effect(state, "navigate", candidate, invoke)

    async def _observe_transit(self, state, navigation):
        """One fresh moving-frame query at a time; a hit only requests stopping.

        A frame must be fresh when submitted. Model latency does not turn it
        into a location estimate: the stopped Graph must obtain and confirm new
        still frames. Moving-frame misses never count as searched coverage.
        """
        from cloud.go2.connection import go2
        from cloud.go2.perception.visual_target import (
            MAX_FRAME_AGE, TargetUnlocalized, detect_visible_target,
            image_quality, save_diagnostic_frame, target_offset,
        )
        last_frame = None
        while not navigation.done():
            reason = await self.guard(state)
            if reason:
                return {"reason": reason}
            captured = go2.latest_frame_snapshot()
            if captured is None:
                await asyncio.sleep(.1)
                continue
            jpeg, frame = captured
            stamp = frame.get("received_at")
            if (frame.get("frame_id") == last_frame
                    or frame.get("odom_session_id") != self.map_session
                    or type(stamp) not in (float, int)
                    or not 0 <= time.monotonic() - stamp <= MAX_FRAME_AGE):
                await asyncio.sleep(.1)
                continue
            last_frame = frame["frame_id"]
            if await invoke_blocking(image_quality, jpeg) < .5:
                await asyncio.sleep(.1)
                continue
            if self.context.persist:
                await invoke_blocking(save_diagnostic_frame, jpeg, frame)
            try:
                observation = await asyncio.wait_for(
                    detect_visible_target(jpeg, state["target"], frame),
                    timeout=min(OBSERVATION_TIMEOUT_S, max(.01, state["deadline"] - time.time())))
                reason = await self.guard(state)
                if reason:
                    return {"reason": reason}
                if type(observation.get("found")) is not bool:
                    return {"reason": "vision_result_invalid"}
                if observation["found"]:
                    target_offset(observation, frame)
                    return {"frame_id": frame["frame_id"], "received_at": float(stamp),
                            "bbox_1000": observation["bbox_1000"]}
            except TargetUnlocalized as exc:
                return {"reason": exc.reason}
            except asyncio.TimeoutError:
                return {"reason": "observation_timeout"}
            # No backlog and no per-frame fanout on the cloud model.
            await asyncio.sleep(.25)
        return None

    async def approach(self, state):
        from cloud.go2.navigation.target_approach import navigate_to_visible_target
        async def invoke():
            return await asyncio.wait_for(navigate_to_visible_target(state["target"], bounds=self.bounds),
                                          timeout=max(0.01, state["deadline"] - time.time()))
        return await self.effect(state, "approach", {"target": state["target"]}, invoke)

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
        bounds = search_bounds()
    except ValueError as exc:
        return {"result": "blocked", "reason": str(exc), "task_id": task_id}
    if mode not in {"explore", "find_object"} or (mode == "find_object" and not target.strip()):
        return {"result": "rejected", "reason": "invalid_search_request"}
    session = getattr(go2, "_odom_session_id", None)
    if not session:
        return {"result": "blocked", "reason": "map_session_unavailable"}
    scope = configured_search_scope()
    scope_session = scope["session_id"] if scope else os.environ.get("GO2_SEARCH_SESSION_ID")
    if scope_session != session:
        return {"result": "blocked", "reason": "search_scope_session_mismatch", "task_id": task_id}
    adapter = DeviceSearch(context, task_id, bounds, session)
    if mode == "explore":
        # Segments share bounded coverage until scope or localization changes.
        global _exploration_coverage
        coverage_key = (context.session_id, session, bounds)
        if _exploration_coverage is not None and _exploration_coverage[0] == coverage_key:
            adapter.coverage = _exploration_coverage[1]
        else:
            _exploration_coverage = (coverage_key, adapter.coverage)
    checkpointer = FileCheckpointSaver()
    config = {"configurable": {"thread_id": f"go2:search:{context.session_id}:{task_id}"},
              "recursion_limit": SEARCH_MAX_STEPS * 5 + 20}
    # A restarted process cannot certify past physical effects or reuse old
    # coverage. Reconcile by terminating; never blindly resume a movement node.
    existing = await checkpointer.aget_tuple(config)
    if existing is not None:
        values = existing.checkpoint.get("channel_values", {})
        return values.get("result") or {"result": "blocked", "reason": "search_recovery_requires_new_observation",
                                        "task_id": task_id}
    control_lease.acquire(context)
    runtime = runtime_for(go2)
    token = None
    if runtime and runtime.data["life_state"] == "idle":
        token = runtime.begin_life("explore" if mode == "explore" else "play", context.session_id, task_id)
    session_tasks.track(context.session_id, asyncio.current_task())
    state = {"task_id": task_id, "mode": mode, "target": target,
             "map_session": session, "phase": "observe", "steps": 0, "distance_m": 0.0,
             "deadline": deadline, "coverage": {}}
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
        return await _run_search(target, mode=mode, context=context,
                                 task_id=task_id, deadline=deadline)
