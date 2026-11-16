"""猜拳攻灯: role-owned choices, locked throws and receipt-gated progress.

The graph below is the game protocol, not another agent runtime. Decisions use
the existing role models through LangGraph; physical tools retain their normal
policy, task ledgers, leases and hardware completion checks.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from dataclasses import replace
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.runner import invoke_blocking
from .base import Game, activity_allows
from .rps_decisions import Go2Setup, LampSetup, decide, gesture_options
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
    identification: dict
    observation: dict
    target_identification: dict
    proposals: dict
    terms: dict
    round: int
    history: list[dict]
    choices: dict
    winner: str
    arrived: bool
    result: str
    reason: str


class GameStopped(Exception):
    pass


def _available(sender: str, target: str) -> bool:
    return {sender, target} == {LAMP, "go2"}


async def _execute(drive, event: dict, task_id: str) -> None:
    from cloud.agents.go2.workflows.search import search_request
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

    def blocked(reason, text):
        # Drive has already announced task_id before entering the game.
        # Omitting it makes AgentMessenger reject even a preflight failure.
        logger.info("RPS game blocked invitation=%s task=%s reason=%s",
                    invitation_id, task_id, reason)
        get_activity_store().record_event(activity_id, "action_result", unit_id="go2",
            text=text, game_id=GAME_ID, trigger_message_id=invitation_id,
            task_id=task_id, status="blocked")
        messenger.send_result(event["message_id"], "blocked", task_id=task_id, reason=reason)

    agent = get_agent()
    if agent is None:
        blocked("lamp_agent_unavailable", "猜拳暂时无法开局：灯的智能体服务不可用。")
        return
    context = replace(drive._context, expires_at=int(deadline), persist=False)
    lamp_epoch = agent._autonomy_epoch, agent._command_epoch
    # Guessing and performing do not require a navigation map. The full
    # search tool checks fresh mapping/localization if the dog wins.
    odom_session = getattr(go2, "_odom_session_id", None)
    owner = "rps:" + invitation_id
    adjudicated_winner = None
    lamp_off_confirmed = False

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
        # Fresh maps are required again by the settlement movement tool.
        # A temporarily stale map while thinking/dancing is not a new epoch.
        if odom_session is not None and getattr(go2, "_odom_session_id", None) != odom_session:
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
                "lamp_identification": state.get("identification"),
                "target_identification": state.get("target_identification"),
                "round": state.get("round", 1), "history": state.get("history", []),
                "winner": state.get("winner")}

    async def decision(role, phase, state=None):
        check()
        answer = await decide(role, phase, facts(state, role),
            thread_id=f"rps:{invitation_id}:{role}:{phase}:{(state or {}).get('round', 0)}",
            deadline=deadline)
        check()
        return answer

    async def physical(action, params, step):
        nonlocal lamp_off_confirmed
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
               role="lamp", action=action, arguments=params, status=result.get("result", "unknown"),
               task_id=result.get("task_id", physical_id))
        if result.get("result") != "ok":
            raise GameStopped("lamp_action_unconfirmed:" + str(result.get("error") or result.get("result")))
        if step == "prize:off":
            # Keep this receipt even if a user intervenes immediately after it.
            lamp_off_confirmed = True
        check()
        return result

    async def identify(state):
        answer = await decision("lamp", "identify", state)
        await physical("SET_STATE", {"state": "ON"}, "identify:power")
        await physical("SET_COLOR", answer["color"], "identify:color")
        # The cue is only handed to the dog after both physical receipts.
        confirmed_at = time.monotonic()
        record("message", answer["speech"], emotion=answer.get("emotion"), role="lamp", stage="identification")
        return {"identification": {"shape": "圆球灯", "color": answer["color"],
                "speech": answer["speech"], "confirmed_at": confirmed_at}}

    async def recognize(state):
        check()
        cue = state["identification"]
        observation = await execute_tool("go2_observe", {
            "question": "用户提供的外形线索：灯是蓝紫色球体，用户会放在狗的视野内；这不是已看见的证明。"
                        "参与游戏的圆球灯已经确认亮出识别色。它告诉你："
                        + cue["speech"] + "。已确认的RGB颜色为" + str(cue["color"]) + "。"
                        "请综合球形外观、蓝紫色线索、实际发光和当前识别色找出候选，描述画面位置、外观或灯旁固定标记。"
                        "蓝紫色是用户提供的外观线索，不要求发光后仍保持蓝紫色；相机呈色与指令RGB也可能不同，"
                        "不要仅因色调不完全相同就排除，但不能仅凭颜色认定身份。"
                        "找不到、外形证据不足或有多个无法区分的候选就明确说明。"
                        "同伴自述不是观察事实，不能把其他同色物品当成灯。"
                        "后续灯会为出拳变色，请同时描述不依赖当前颜色的固定特征。"},
            context=context, observation_after=cue["confirmed_at"])
        if observation.get("result") != "ok":
            raise GameStopped("game_observation_unavailable")
        text = str(observation.get("message") or "").strip()
        if not text:
            raise GameStopped("game_observation_unavailable")
        observed = record("observation", text[:800], source="go2.camera")
        check()
        observed_state = {**state, "identification": cue,
                         "observation": {"text": text[:1600], "source": "go2.camera",
                         "as_of": time.time(), "ref": (observed or {}).get("event_id")}}
        dog = await decision("go2", "identify", observed_state)
        record("message", dog["speech"], emotion=dog.get("emotion"), stage="recognition")
        if not dog["target_visible"] or not dog.get("target"):
            return {**observed_state, "result": "rejected", "reason": "game_target_not_visible"}
        return {**observed_state, "target_identification": dog}

    async def setup(state):
        lamp, dog = await asyncio.gather(decision("lamp", "setup", state),
                                         decision("go2", "setup", state))
        # Validate both proposals before publishing either; these public rules
        # are not private throws and must be visible even if agreement fails.
        lamp = LampSetup.model_validate(lamp).model_dump()
        dog = Go2Setup.model_validate(dog).model_dump()
        check()
        terms = {"lamp_mapping": lamp["mapping"], "lamp_color_names": lamp["color_names"],
                 "go2_mapping": dog["mapping"]}
        gestures = gesture_options()
        for role, answer in (("lamp", lamp), ("go2", dog)):
            labels = (lamp["color_names"] if role == "lamp" else
                      {choice: gestures[command]["label"] for choice, command in dog["mapping"].items()})
            mapping_text = "；".join(f"{label}＝{labels[choice]}" for choice, label in CHOICES.items())
            record("message", f"本局映射提议：{mapping_text}。这是公开规则，待双方确认。\n{answer['speech']}",
                   role=role, stage="mapping_proposal", emotion=answer.get("emotion"))
        record("message", "待确认的规则：一把定胜负，平局重来。狗赢就沿安全路径到本局确认的灯附近停稳，"
               "抬前腿象征性按灯，动作确认完成后关灯；"
               "灯赢就由狗表演一次。双方确认映射后先认灯定位，再秘密选拳、同时展示。", stage="terms_proposal")
        return {"proposals": {"lamp": lamp, "go2": dog}, "terms": terms,
                "round": 1, "history": [], "arrived": False}

    async def agree(state):
        lamp, dog = await asyncio.gather(decision("lamp", "agree", state),
                                         decision("go2", "agree", state))
        for role, answer in (("lamp", lamp), ("go2", dog)):
            record("message", answer["speech"], emotion=answer.get("emotion"), role=role, stage="agreement",
                   status="accepted" if answer["accepted"] else "rejected")
        if not lamp["accepted"] or not dog["accepted"]:
            return {"result": "rejected", "reason": "game_terms_not_agreed"}
        record("message", "双方已确认本局映射和规则，接下来亮识别色、确认灯的位置，然后秘密选拳、同时展示。", stage="terms_confirmed")
        return {}

    async def choose(state):
        # Both receive only PUBLIC history. Neither receives the opponent's
        # current choice, even if its model call happens to finish first.
        lamp, dog = await asyncio.gather(decision("lamp", "throw", state),
                                         decision("go2", "throw", state))
        check()
        return {"choices": {"lamp": lamp, "go2": dog}}

    async def reveal(state):
        nonlocal adjudicated_winner
        check()
        choices, terms, number = state["choices"], state["terms"], state["round"]
        current = (agent._state.actuator_snapshot().get(LAMP, {}).get("state") or {})
        if current.get("ism") != "ON":
            await physical("SET_STATE", {"state": "ON"}, f"{number}:power")
        color = terms["lamp_mapping"][choices["lamp"]["choice"]]
        command = terms["go2_mapping"][choices["go2"]["choice"]]
        async def dog_gesture():
            check()
            result = await execute_tool("go2_sport", {"cmd": command}, context=context,
                                       request_id=f"rps_{invitation_id}_{number}_gesture")
            record("action_result", f"狗的出拳动作{('已确认' if result.get('result') == 'ok' else '未确认')}。",
                   action="go2_sport", arguments={"cmd": command}, status=result.get("result", "unknown"),
                   task_id=result.get("command_request_id") or task_id)
            if result.get("result") != "ok":
                raise GameStopped("go2_gesture_unconfirmed:" + str(result.get("error") or result.get("result")))
            check()

        record("message", f"第 {number} 回合：双方已锁定选择，同时开始展示。", stage="reveal")
        for role in ("lamp", "go2"):
            record("message", choices[role]["speech"], role=role, stage="throw_announcement", emotion=choices[role].get("emotion"))
        # Dispatch both without waiting for either receipt. Collect both
        # outcomes before scoring; never leave a sibling task unobserved.
        results = await asyncio.gather(
            physical("SET_COLOR", color, f"{number}:color"), dog_gesture(), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        check()
        winner = round_winner(choices["lamp"]["choice"], choices["go2"]["choice"])
        if winner != "draw":
            adjudicated_winner = winner
        label = {"go2": "狗赢，接下来靠近灯并关灯", "lamp": "灯赢，接下来由狗表演", "draw": "平局，重新出拳"}[winner]
        record("message", f"第 {number} 回合：灯出{CHOICES[choices['lamp']['choice']]}，"
               f"狗出{CHOICES[choices['go2']['choice']]}；{label}。",
               stage="rps_result", reason=winner + "_won" if winner != "draw" else "draw",
               game_round={"round": number,
                   "choices": {LAMP: choices["lamp"]["choice"], "go2": choices["go2"]["choice"]},
                   "mapping": {
                       LAMP: {"choice": choices["lamp"]["choice"], "color": color,
                              "label": terms["lamp_color_names"][choices["lamp"]["choice"]]},
                       "go2": {"choice": choices["go2"]["choice"], "action": "go2_sport",
                               "arguments": {"cmd": command}, "label": gesture_options()[command]["label"]}},
                   "winner": LAMP if winner == "lamp" else winner})
        history = [*state["history"], {"round": number, "lamp": choices["lamp"]["choice"],
                   "go2": choices["go2"]["choice"], "winner": winner}]
        return {"winner": winner, "history": history}

    async def approach(state):
        check()
        target = state["target_identification"]["target"]
        search_id = "rps_" + invitation_id + "_lamp"
        record("action", "我赢了，准备边走边观察约定的灯，确认已在附近并停稳后关灯。",
               action="go2_find_object", task_id=search_id)
        with search_request(search_id, deadline):
            result = await execute_tool("go2_find_object",
                {"target": target}, context=context)
        record("action_result", str(result.get("message") or result.get("reason") or result.get("result")),
               action="go2_find_object", status=result.get("result", "unknown"),
               task_id=result.get("task_id") or search_id)
        if (result.get("result") != "ok" or result.get("outcome") != "nearby"
                or result.get("found") is not True or (result.get("stop") or {}).get("result") != "ok"
                or result.get("target") != target or result.get("task_id") != search_id):
            raise GameStopped("game_approach_unconfirmed:" + str(result.get("reason") or result.get("error") or result.get("result")))
        check()
        # Search proves visual proximity and stopping, not a metric lamp position.
        return {"arrived": True}

    async def switch_off(state):
        check()
        if not state.get("arrived"):
            raise GameStopped("arrival_not_confirmed")
        gesture_id = f"rps_{invitation_id}_switch_off_gesture"
        record("action", "已在约定目标附近停稳并复核，准备抬前腿象征性按一下灯。",
               action="go2_sport", arguments={"cmd": "Hello"}, task_id=gesture_id)
        result = await execute_tool("go2_sport", {"cmd": "Hello"}, context=context,
                                    request_id=gesture_id)
        record("action_result", "象征性按灯的抬前腿动作" +
               ("已确认完成。" if result.get("result") == "ok" else "尚未确认完成。"),
               action="go2_sport", arguments={"cmd": "Hello"}, status=result.get("result", "unknown"),
               task_id=result.get("command_request_id") or gesture_id)
        if result.get("result") != "ok":
            raise GameStopped("game_switch_off_gesture_unconfirmed:" + str(result.get("error") or result.get("result")))
        check()
        record("message", "抬前腿动作已确认完成，现在兑现本局的关灯约定。")
        await physical("SET_STATE", {"state": "OFF"}, "prize:off")
        return {"result": "ok", "reason": "go2_won_lamp_off_confirmed"}

    async def perform(state):
        answer = await decision("go2", "performance", state)
        check()
        record("message", answer["speech"], emotion=answer.get("emotion"))
        record("action", "狗准备兑现本局约定，表演一个动作。", action="go2_sport")
        result = await execute_tool("go2_sport", {"cmd": answer["command"]}, context=context,
            request_id=f"rps_{invitation_id}_performance")
        record("action_result", "狗的表演" + ("已确认完成。" if result.get("result") == "ok" else "尚未确认完成。"),
               action="go2_sport", status=result.get("result", "unknown"),
               task_id=result.get("command_request_id") or task_id)
        if result.get("result") != "ok":
            raise GameStopped("game_performance_unconfirmed:" + str(result.get("error") or result.get("result")))
        check()
        return {"result": "ok", "reason": "lamp_won_performance_confirmed"}

    def next_round(state):
        return {"round": state["round"] + 1, "choices": {}}

    graph = StateGraph(SiegeState)
    for name, node in (("identify", identify), ("recognize", recognize), ("setup", setup), ("agree", agree), ("choose", choose),
                       ("reveal", reveal), ("approach", approach), ("switch_off", switch_off), ("perform", perform),
                       ("next_round", next_round)):
        graph.add_node(name, node)
    graph.add_edge(START, "setup")
    graph.add_edge("identify", "recognize")
    graph.add_conditional_edges("recognize", lambda s: END if s.get("result") else "choose")
    graph.add_edge("setup", "agree")
    graph.add_conditional_edges("agree", lambda s: END if s.get("result") else "identify")
    graph.add_edge("choose", "reveal")
    graph.add_conditional_edges("reveal", lambda s: {"go2": "approach", "lamp": "perform", "draw": "next_round"}[s["winner"]])
    graph.add_edge("approach", "switch_off")
    graph.add_edge("next_round", "choose")
    graph.add_edge("switch_off", END)
    graph.add_edge("perform", END)

    if not agent.begin_peer_game(owner):
        blocked("lamp_busy", "猜拳暂时无法开局：灯正在处理其他任务或等待动作确认。")
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
                {"configurable": {"thread_id": thread_id}, "recursion_limit": sys.maxsize}, durability="sync"))
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
            agent.end_peer_game(owner, preserve_output=lamp_off_confirmed)
        if adjudicated_winner and status != "ok":
            reason = adjudicated_winner + "_won:" + reason
        await drive._advance_task(goal, stage="finish", result={"result": status, "reason": reason,
                                                              "game_winner": adjudicated_winner})
    text = {"go2_won_lamp_off_confirmed": "猜拳攻灯结束：狗猜拳获胜，已确认在约定的灯附近停稳、抬前腿象征性按灯并关灯。",
            "lamp_won_performance_confirmed": "猜拳攻灯结束：灯猜拳获胜，狗已完成约定的表演。"}.get(reason)
    if text is None:
        text = ("猜拳结果已确定：" + ("狗获胜" if adjudicated_winner == "go2" else "灯获胜")
                + "；后续约定动作未确认完成或已中断：" + reason if adjudicated_winner else
                "猜拳攻灯已中断或未达成开局约定，本局未决出胜负：" + reason)
    record("message", text, stage="settlement", reason=reason, status=status)
    messenger.send_result(event["message_id"], status, task_id=task_id, reason=reason)


GAME = Game(
    game_id=GAME_ID, name="猜拳攻灯",
    description="灯与狗自选颜色和动作映射，一把定胜负，平局重来；狗赢过去关灯，灯赢狗表演。",
    participants=(LAMP, "go2"), bidirectional=True, requires_idle=True,
    available=_available, execute=_execute, on_invitation=on_invitation,
    on_acceptance=on_acceptance, acceptance="", invitation_ttl_s=900,
    rules=("灯或狗均可自主邀请，双方同处活动且空闲，对方自主接受或拒绝。"
           "流程为接受参与→公开双方映射→确认规则→认灯定位→秘密选拳并同时展示。接受邀请只同意进入准备，"
           "不要求此时灯已亮起或已经认出灯；上一局认灯失败不能代替本局准备后的新观察。"
           "开局各自选择石头、剪刀、布的三种表达：灯自选三个可区分的亮灯颜色，狗自选三个当前可执行动作；"
           "用户提供灯是蓝紫色球体、会放在狗视野内的线索。规则确认后灯自主选择识别色并真实亮灯，再告诉狗外形及颜色；狗用亮灯后的新画面辨认和定位。"
           "用户线索不能代替观察；综合球形、蓝紫色外观线索、发光与识别色辨认，色调差异不能单独否定候选，也不能只凭同色认定身份；观察不足或候选不唯一则不出拳。"
           "约定目标保留圆球灯的位置与固定特征，猜拳变色不更换目标。"
           "先各自选映射，逐项公开石头、剪刀、布对应的颜色或动作，再分别确认映射与规则；随后认灯定位，确认成功才秘密选拳。"
           "映射是双方和用户都能读到的公开规则，不能用保密出拳为由隐瞒。"
           "不规定固定映射，双方确认后本局冻结，不协商回合数或推进距离。"
           "双方私下锁定选择后并发派发变色和动作，不等待一方完成才派发另一方；设备与网络存在时延，不承诺物理上精确同步。不能根据对方本轮选择改拳。石头胜剪刀、剪刀胜布、布胜石头；"
           "一把定胜负，平局原地重来，直到分出胜负或本局期限到达。展示须灯物理回执与狗真实动作反馈均确认才能判拳。"
           "狗赢后用找物流程接近约定的圆球灯，沿可通行路径分段移动、停稳再看；"
           "连续新鲜画面确认已在附近且真实停稳后，执行Hello挥手抬前腿象征性按灯，"
           "等待真实动作完成反馈后才发送关灯命令，不要求实际碰灯；"
           "不要求雷达分离出灯的点簇或精确目标距离，须真实关灯回执；"
           "灯赢后由狗结合当前条件选择一个支持的表演动作，可以跳舞或其他动作，须真实完成回执。"
           "猜拳胜负与后续动作完成分开记录，导航、抬前腿、关灯或表演失败不能改判输赢，未确认不能声称兑现成功。"
           "游戏期间灯暂停普通自主调光；用户介入、活动结束、断连、定位会话改变或期限到达则中断，"
           "动作未知按原任务核对，重启不重放旧游戏。本局履约完成后释放游戏占用，保留原自主许可，"
           "由既有自主Graph继续自由探索；用户接管或活动结束不恢复。"
           "关灯确认后保持该结果直到新的有效传感器机会，不因收尾立即自行开灯。"),
    strategy=("结合人格、当前活动、用户要求和新鲜观察决定邀请或接受，不能因可玩列表存在就强制开局。"
              "自行选择易辨认的表达并解释本局约定；可结合已公开的历史出拳调整策略，但不得获取对方本轮私下选择。"
              "邀请阶段判断参与意愿与当前占用；接受后先公开映射并确认规则，再亮灯辨认目标，不能要求先认灯才制定规则。"
              "认灯阶段须有本局新画面支持，动作与导航分别遵守实时执行门禁；游戏中不临时改映射或规则。"
              "交流沿用自身人格，不预排对白、固定出拳序列或庆祝动作。"),
)
