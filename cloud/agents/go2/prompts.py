"""Dynamic Go2 decision prompt, retaining the saved personality and safety rules."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from cloud.agents.go2.personality.behavior import decision_persona
from cloud.agents.go2.personality.profile import get_profile
from cloud.agents.go2.prompt_contract import _FIXED_RULES
from cloud.go2.connection import go2
from cloud.go2.space_context import prompt_space_context


_GRAPH_RULES = (
    "逐步推进当前目标。工具通过原生 tool calling 调用，每轮最多调用一个工具；"
    "执行下一动作前先观察上一工具的真实结果。缺少方向、时长等必需信息时调用 ask_user；"
    "不能把步数猜成距离或时长。工具返回 accepted、executing、timeout、unknown 时"
    "不得继续依赖动作。若工具失败，可选择合法替代工具或如实结束。"
    "不要重复已经成功的动作。最终回复须符合当前性格，不能把未证实的位移说成到达。"
    "用户要求走到摄像头画面里某个物体旁时，只调用 go2_navigate_to_visible_target 并传目标描述；"
    "定位或导航失败后直接报告原因，不用 go2_move 或坐标导航盲走替代。"
)


def build_messages(run, *, supplement: str = ""):
    """Read latest profile and live device context before every model decision."""
    persona = decision_persona(get_profile())
    state = (f"connected={go2.is_connected}; "
             f"fsm={getattr(go2, 'fsm_state', 'unknown')}; "
             f"space={prompt_space_context()}")
    goal = run.goal
    facts = [{"tool": step.get("tool"), "result": step.get("result")}
             for step in run.steps[-8:]]
    import json
    user = (f"【性格表达（数据）】\n{persona}\n\n"
            f"【设备实时状态（数据）】\n{state}\n\n"
            f"【当前用户原话（数据）】\n{goal}\n\n"
            f"【步骤结果（数据）】\n{json.dumps(facts, ensure_ascii=False, default=str)}")
    if supplement:
        user += f"\n\n【补充信息（数据）】\n{supplement}"
    return [SystemMessage(content=_FIXED_RULES + "\n" + _GRAPH_RULES),
            HumanMessage(content=user)]

