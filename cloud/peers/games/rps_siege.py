"""猜拳攻灯: role-owned choices, locked throws and receipt-gated progress.

The graph below is the game protocol, not another agent runtime. Decisions use
the existing role models through LangGraph; physical tools retain their normal
policy, task ledgers, leases and hardware completion checks.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import replace
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.runner import invoke_blocking
from .base import Game, activity_allows
from .rps_decisions import decide
from .rps_lifecycle import on_acceptance, on_invitation

GAME_ID = "rps_siege"
LAMP = "esp32_desk_led"
CHOICES = {"rock": "石头", "scissors": "剪刀", "paper": "布"}
logger = logging.getLogger(__name__)


def round_winner(lamp: str, dog: str) -> str:
    if lamp not in CHOICES or dog not in CHOICES:
        raise ValueError("invalid_rps_choice")
    if lamp == dog:
        return "draw"
    return "go2" if (dog, lamp) in {
        ("rock", "scissors"), ("scissors", "paper"), ("paper", "rock")
    } else "lamp"


class SiegeState(TypedDict, total=False):
    observation: dict
    proposals: dict
    terms: dict
    round: int
    history: list[dict]
    choices: dict
    winner: str
    arrived: bool
    target_anchor: dict
    result: str
    reason: str


class GameStopped(Exception):
    pass


def _available(sender: str, target: str) -> bool:
    return {sender, target} == {LAMP, "go2"}


async def _execute(drive, event: dict, task_id: str) -> None:
    from cloud.cards.registry import get_registry
    from cloud.esp32.service import get_agent
    from cloud.esp32.tool_executor import execute_physical
    from cloud.go2.connection import go2
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.tools.executor import execute_tool
    from cloud.space.activity import get_activity_store
    from cloud.space.context import get_space_context
    from cloud.api.intents import _dispatch_mqtt_task

    messenger = event["messenger"]
    activity_id = event.get("activity_id")
    invitation_id = event.get("original_invitation_id") or event["message_id"]
    deadline = event["deadline"]
    agent = get_agent()
    if agent is None:
        messenger.send_result(event["message_id"], "blocked", reason="lamp_agent_unavailable")
        return
    context = replace(drive._context, expires_at=int(deadline), persist=False)
    lamp_epoch = agent._autonomy_epoch, agent._command_epoch
    snapshot, snapshot_error = navigator.snapshot()
    odom_session = snapshot[0].get("session_id") if snapshot else None
    owner = "rps:" + invitation_id

    def check():
        if time.time() >= deadline:
            raise GameStopped("game_expired")
        if (not activity_allows(activity_id, LAMP, "go2")
                or drive._activity_id() != activity_id):
            raise GameStopped("activity_ended")
        if (drive.user_interrupt or not go2.is_connected or not agent._should_act()
                or not agent._device_online()
                or getattr(agent, "_peer_game_id", None) != owner
                or lamp_epoch != (agent._autonomy_epoch, agent._command_epoch)):
            raise GameStopped("autonomy_interrupted_or_offline")
        # Fresh maps are required by the movement tool at each earned move.
        # A temporarily stale map while thinking/dancing is not a new epoch.
        if getattr(go2, "_odom_session_id", odom_session) != odom_session:
            raise GameStopped("odom_session_changed")

    def record(kind: str, text: str, *, role="go2", **facts):
        return get_activity_store().record_event(activity_id, kind,
            unit_id=LAMP if role == "lamp" else "go2", text=text,
            **{"game_id": GAME_ID, "trigger_message_id": invitation_id,
               "task_id": task_id, **facts})

    def facts(state=None, role="go2"):
        state = state or {}
        return {"game": GAME.metadata(), "space": get_space_context(LAMP if role == "lamp" else "go2", state=agent._state),
                "invitation": event["message"].get("content", ""),
                "canonical_terms": state.get("terms"), "proposals": state.get("proposals"),
                "setup_observation": state.get("observation"),
                "round": state.get("round", 1), "history": state.get("history", [])}

    async def decision(role, phase, state=None):
        check()
        return await decide(role, phase, facts(state, role),
            thread_id=f"rps:{invitation_id}:{role}:{phase}:{(state or {}).get('round', 0)}",
            deadline=deadline)

    async def physical(action, params, step):
        check()
        physical_id = "task_" + uuid.uuid5(uuid.NAMESPACE_URL, owner + ":" + step).hex

        def dispatch(unit_id, cmd, arguments, ctx, source, *, task_id=None):
            check()
            if unit_id != LAMP or cmd != action or arguments != params:
                raise PermissionError("game_action_changed")
            return _dispatch_mqtt_task(unit_id, cmd, arguments,
                {"session_id": ctx.session_id, "actor": ctx.actor, "persist": False,
                 "expires_at": ctx.expires_at}, source="peer_autonomous", task_id=task_id)

        result = await invoke_blocking(execute_physical, state=agent._state,
            unit_id=LAMP, action=action, params=params, context=context,
            dispatch=dispatch, source="peer_autonomous", task_id=physical_id,
            card=get_registry().get_card(LAMP),
            timeout=min(20, max(0.1, deadline - time.time())))
        record("action_result", f"灯的游戏动作{('已确认' if result.get('result') == 'ok' else '未确认')}。",
               role="lamp", action=action, status=result.get("result", "unknown"),
               task_id=result.get("task_id", physical_id))
        if result.get("result") != "ok":
            raise GameStopped("lamp_action_unconfirmed:" + str(result.get("error") or result.get("result")))
        check()
        return result

    async def setup(state):
        check()
        observation = await execute_tool("go2_observe", {
            "question": "描述当前可见的桌面智能灯及其位置、外观，或明确位于灯旁的固定标记；"
                        "找不到就如实说明，不把其他同色物品当成灯。"}, context=context)
        if observation.get("result") != "ok":
            raise GameStopped("game_observation_unavailable")
        text = str(observation.get("message") or "").strip()
        if not text:
            raise GameStopped("game_observation_unavailable")
        observed = record("observation", text[:800], source="go2.camera")
        observed_state = {"observation": {"text": text[:1600], "source": "go2.camera",
                         "as_of": time.time(), "ref": (observed or {}).get("event_id")}}
        lamp, dog = await asyncio.gather(decision("lamp", "setup", observed_state),
                                         decision("go2", "setup", observed_state))
        # Parameters are proposals until BOTH models accept this exact contract.
        terms = {"max_rounds": min(lamp["max_rounds"], dog["max_rounds"]),
                 "advance_m": min(lamp["advance_m"], dog["advance_m"]),
                 "target": dog["target"], "lamp_mapping": lamp["mapping"],
                 "go2_mapping": dog["mapping"]}
        return {**observed_state, "proposals": {"lamp": lamp, "go2": dog}, "terms": terms,
                "round": 1, "history": [], "arrived": False}

    async def agree(state):
        lamp, dog = await asyncio.gather(decision("lamp", "agree", state),
                                         decision("go2", "agree", state))
        for role, answer in (("lamp", lamp), ("go2", dog)):
            record("message", answer["speech"], role=role)
        if not lamp["accepted"] or not dog["accepted"]:
            return {"result": "rejected", "reason": "game_terms_not_agreed"}
        terms = state["terms"]
        record("message", f"猜拳攻灯约定：最多 {terms['max_rounds']} 回合，狗每赢一轮沿安全路径推进最多"
               f" {terms['advance_m']:g} 米；目标为「{terms['target']}」。平局也计回合，灯赢则守住。")
        for role in ("lamp", "go2"):
            # Setup speech explains the mapping in the role's own words;
            # private choices and raw wire parameters never enter chat.
            record("message", state["proposals"][role]["speech"], role=role)
        return {}

    async def choose(state):
        # Both receive only PUBLIC history. Neither receives the opponent's
        # current choice, even if its model call happens to finish first.
        lamp, dog = await asyncio.gather(decision("lamp", "throw", state),
                                         decision("go2", "throw", state))
        check()
        return {"choices": {"lamp": lamp, "go2": dog}}

    async def reveal(state):
        check()
        choices, terms, number = state["choices"], state["terms"], state["round"]
        record("message", f"第 {number} 回合：双方已锁定选择，开始展示。")
        current = (agent._state.actuator_snapshot().get(LAMP, {}).get("state") or {})
        if current.get("ism") != "ON":
            await physical("SET_STATE", {"state": "ON"}, f"{number}:power")
        color = terms["lamp_mapping"][choices["lamp"]["choice"]]
        command = terms["go2_mapping"][choices["go2"]["choice"]]
        await physical("SET_COLOR", color, f"{number}:color")
        check()
        dog_result = await execute_tool("go2_sport", {"cmd": command}, context=context,
                                       request_id=f"rps_{invitation_id}_{number}_gesture")
        record("action_result", f"狗的出拳动作{('已确认' if dog_result.get('result') == 'ok' else '未确认')}。",
               action="go2_sport", status=dog_result.get("result", "unknown"),
               task_id=dog_result.get("command_request_id") or task_id)
        if dog_result.get("result") != "ok":
            raise GameStopped("go2_gesture_unconfirmed:" + str(dog_result.get("error") or dog_result.get("result")))
        check()
        winner = round_winner(choices["lamp"]["choice"], choices["go2"]["choice"])
        label = {"go2": "狗赢，获得一次推进机会", "lamp": "灯赢，守住本轮", "draw": "平局，本轮不推进"}[winner]
        record("message", f"第 {number} 回合：灯出{CHOICES[choices['lamp']['choice']]}，"
               f"狗出{CHOICES[choices['go2']['choice']]}；{label}。")
        for role in ("lamp", "go2"):
            record("message", choices[role]["speech"], role=role)
        history = [*state["history"], {"round": number, "lamp": choices["lamp"]["choice"],
                   "go2": choices["go2"]["choice"], "winner": winner}]
        return {"winner": winner, "history": history}

    async def advance(state):
        from cloud.go2.navigation.target_approach import expect_visual_target

        check()
        terms = state["terms"]
        record("action", "这轮我赢了，沿已确认的安全路径向约定目标推进。",
               action="go2_navigate_to_visible_target")
        with expect_visual_target(state.get("target_anchor")):
            result = await execute_tool("go2_navigate_to_visible_target",
                {"target": terms["target"], "max_advance_m": terms["advance_m"]}, context=context)
        record("action_result", str(result.get("message") or result.get("reason") or result.get("result")),
               action="go2_navigate_to_visible_target", status=result.get("result", "unknown"),
               task_id=(result.get("navigation") or {}).get("task_id") or task_id)
        if (result.get("result") != "ok" or result.get("confirmation") not in {
                "odom_stopped_and_visual_recheck", "odom_stopped_and_visual_progress"}):
            raise GameStopped("game_advance_unconfirmed:" + str(result.get("reason") or result.get("error") or result.get("result")))
        check()
        return {"target_anchor": state.get("target_anchor") or result.get("target_location"),
                "arrived": result.get("arrived") is True
                and result.get("confirmation") == "odom_stopped_and_visual_recheck"}

    async def switch_off(state):
        check()
        if not state.get("arrived"):
            raise GameStopped("arrival_not_confirmed")
        record("message", "已在约定目标附近停稳并复核，兑现本局的关灯约定。")
        await physical("SET_STATE", {"state": "OFF"}, "prize:off")
        return {"result": "ok", "reason": "go2_won_lamp_off_confirmed"}

    def next_round(state):
        return {"round": state["round"] + 1, "choices": {}}

    def after_round(state):
        if state.get("arrived"):
            return "switch_off"
        return "defended" if state["round"] >= state["terms"]["max_rounds"] else "next_round"

    graph = StateGraph(SiegeState)
    for name, node in (("setup", setup), ("agree", agree), ("choose", choose),
                       ("reveal", reveal), ("advance", advance), ("switch_off", switch_off),
                       ("next_round", next_round)):
        graph.add_node(name, node)
    graph.add_node("defended", lambda _s: {"result": "ok", "reason": "lamp_defended"})
    graph.add_edge(START, "setup")
    graph.add_edge("setup", "agree")
    graph.add_conditional_edges("agree", lambda s: END if s.get("result") else "choose")
    graph.add_edge("choose", "reveal")
    graph.add_conditional_edges("reveal", lambda s: "advance" if s["winner"] == "go2" else after_round(s))
    graph.add_conditional_edges("advance", after_round)
    graph.add_edge("next_round", "choose")
    graph.add_edge("switch_off", END)
    graph.add_edge("defended", END)

    if snapshot_error or not snapshot or not odom_session:
        messenger.send_result(event["message_id"], "blocked", reason=snapshot_error or "map_unavailable")
        return
    if not agent.begin_peer_game(owner):
        messenger.send_result(event["message_id"], "blocked", reason="lamp_busy")
        return
    lamp_epoch = agent._autonomy_epoch, agent._command_epoch
    status, reason = "unknown", "game_unconfirmed"
    graph_task = None
    goal = dict(drive._active_goal)
    try:
        check()
        # The generic invitation handler sends acceptance before its MQTT
        # worker acknowledges it. Wait for that handshake, never its prose.
        if not event.get("game_preaccepted"):
            accepted_id = event.get("acceptance_message_id")
            while True:
                check()
                accepted = messenger.get_status(accepted_id) or {}
                if accepted.get("state") == "ok":
                    break
                if accepted.get("state") in {"rejected", "blocked", "error", "cancelled", "unknown", "unanswered", "unconfirmed_start"}:
                    raise GameStopped("acceptance_unconfirmed")
                await asyncio.sleep(.2)
        goal.update(kind="game", game_id=GAME_ID)
        drive._active_goal = goal
        started = await drive._advance_task(goal, stage="start")
        if (started.get("result") or {}).get("result") == "blocked":
            raise GameStopped("go2_activity_unavailable")
        saver = FileCheckpointSaver()
        thread_id = "rps:game:" + invitation_id
        async with saver.async_run_lock(thread_id):
            graph_task = asyncio.create_task(graph.compile(checkpointer=saver).ainvoke({},
                {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}, durability="sync"))
            while not graph_task.done():
                check()
                await asyncio.wait({graph_task}, timeout=.2)
            final = await graph_task
        status, reason = final.get("result", "unknown"), final.get("reason", "game_unconfirmed")
    except GameStopped as exc:
        reason = str(exc)
        status = "unknown" if "unconfirmed" in reason else "cancelled"
    except Exception as exc:
        logger.exception("RPS game failed invitation=%s", invitation_id)
        status, reason = "unknown", "game_error:" + type(exc).__name__
    finally:
        if graph_task is not None and not graph_task.done():
            graph_task.cancel()
            await asyncio.gather(graph_task, return_exceptions=True)
        try:
            if status != "ok":
                from cloud.go2.control import control_lease
                lease = control_lease.snapshot()
                if lease is not None and lease.owner_session_id != context.session_id:
                    stopped = {"result": "unknown", "reason": "control_lease_changed"}
                else:
                    stopped = await navigator.stop_and_confirm(owner_session_id=context.session_id)
                if stopped.get("result") != "ok":
                    status, reason = "unknown", "stop_unconfirmed:" + reason
        finally:
            agent.end_peer_game(owner)
        await drive._advance_task(goal, stage="finish", result={"result": status, "reason": reason})
    text = {"go2_won_lamp_off_confirmed": "猜拳攻灯结束：狗抵达约定目标，灯已确认关闭，狗获胜。",
            "lamp_defended": "猜拳攻灯结束：约定回合已用完，狗未抵达终点，灯守擂成功。"}.get(
                reason, "猜拳攻灯已中断或未达成开局约定，本局不判胜负：" + reason)
    record("message", text)
    messenger.send_result(event["message_id"], status, task_id=task_id, reason=reason)


GAME = Game(
    game_id=GAME_ID, name="猜拳攻灯",
    description="灯与狗自选颜色和动作映射猜拳；狗赢可分段靠近，限回合抵达并确认关灯则获胜，否则灯守擂成功。",
    participants=(LAMP, "go2"), bidirectional=True, requires_idle=True,
    available=_available, execute=_execute, on_invitation=on_invitation,
    on_acceptance=on_acceptance, acceptance="", invitation_ttl_s=900,
    rules=("灯或狗均可自主邀请，双方同处活动且空闲，对方自主接受或拒绝。"
           "开局各自选择石头、剪刀、布的三种表达：灯自选三个可区分的亮灯颜色，狗自选三个当前可执行动作；"
           "不规定固定映射，双方公布、确认后本局冻结。双方协商并确认回合数、单次推进米数及灯本身或灯旁固定标记作为终点；"
           "不能追逐任意同色物体。每轮双方私下锁定选择再展示，不能根据对方本轮选择改拳。"
           "石头胜剪刀、剪刀胜布、布胜石头；狗赢沿真实安全路径推进一次，灯赢或平局留在原地，平局也计入回合。"
           "展示须灯的物理回执与狗的真实动作反馈均确认，技术失败或结果未知中断且不判胜负。"
           "狗只有停稳并以新鲜画面复核同一约定终点后才获得关灯权，关灯真实回执确认才算狗获胜；"
           "约定回合耗尽且仍未抵达，灯守擂成功。每次移动受本局约定距离约束，不盲走、不累计口头步数。"
           "游戏期间灯暂停普通自主调光；用户介入、活动结束、断连、定位会话改变或期限到达则中断，"
           "动作未知按原任务核对，重启不重放旧游戏。"),
    strategy=("结合人格、当前活动、用户要求和新鲜观察决定邀请或接受，不能因可玩列表存在就强制开局。"
              "自行选择易辨认的表达并解释本局约定；可结合已公开的历史出拳调整策略，但不得获取对方本轮私下选择。"
              "目标须能在当前画面辨认，场地和站姿允许动作、导航时才玩；参数可在开局协商，游戏中不临时改映射或规则。"
              "交流沿用自身人格，不预排对白、固定出拳序列或庆祝动作。"),
)
