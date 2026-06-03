import json
import os
import uuid
import time as _time

from fastapi import APIRouter
from pydantic import BaseModel
from openai import OpenAI

router = APIRouter()

_client = OpenAI(
    base_url=os.getenv("CHAT_API_BASE_URL", "https://tokenhub.tencentmaas.com/v1"),
    api_key=os.getenv("CHAT_API_KEY"),
)
_MODEL = os.getenv("CHAT_MODEL", "hy3-preview")

_NLU_SYSTEM = """你是 SSM 智能家居语音助手的意图解析器。将用户输入解析为结构化 JSON。

首先判断 intent_type：
- "execute"：立即执行的一次性指令（"把灯调暗"、"开灯"、"播放警报"）
- "define_rule"：定义自动化规则（含"以后"、"每次"、"当…就"、"检测到…就"、"自动"等词）

execute 输出格式（只输出 JSON，不要代码块和解释）：
{
  "intent_type": "execute",
  "nlu_feedback": "好的，我来帮你调暗灯光。",
  "requirements": [{"resource_tag": "lighting", "action": "dim", "context": ""}]
}

define_rule 输出格式（只输出 JSON，不要代码块和解释）：
{
  "intent_type": "define_rule",
  "nlu_feedback": "明白了，我来帮你设置这条规则。",
  "rule": {
    "name": "检测到人就开灯",
    "trigger": {"agent_tag": "presence", "event": "detected"},
    "action": {"resource_tag": "lighting", "cmd": "SET_STATE", "params": {"state": "BRIGHT"}}
  }
}

trigger.agent_tag 选择：presence（存在/红外）、light_level（光线）、sound（声音）
trigger.event 选择：
  presence → detected（检测到人）、disappeared（人离开）
  light_level → dark（变暗）、bright（变亮）
  sound → detected（检测到声音）

action.resource_tag 选择：lighting（灯光）、ambiance（氛围）、alert（警报）、notification（通知）
action.cmd 与 params：
  SET_STATE: params={state: "BRIGHT"|"DIM"|"OFF"}
  SET_COLOR: params={r:255, g:160, b:60, brightness:180}（暖光示例）
  BLINK: params={r:255, g:255, b:255, count:3}
  PLAY: params={pattern: "NOTIFY"|"ALERT"}

execute 时 resource_tag 选择：lighting、ambiance、alert、notification
execute 时 action 选择：set_color、brighten、dim、off、on、blink、alert、notify"""


class NLURequest(BaseModel):
    message: str
    devices: list = []


@router.post("/api/intent")
def nlu(req: NLURequest):
    session_id = f"s_{int(_time.time())}_{uuid.uuid4().hex[:6]}"

    response = _client.chat.completions.create(
        model=_MODEL,
        max_tokens=512,
        messages=[
            {"role": "system", "content": _NLU_SYSTEM},
            {"role": "user",   "content": req.message},
        ],
    )

    content = response.choices[0].message.content.strip()
    try:
        result = json.loads(content)
    except Exception:
        result = {"intent_type": "execute", "nlu_feedback": "好的，我来帮你处理。", "requirements": []}

    intent_type = result.get("intent_type", "execute")
    base = {
        "session_id":   session_id,
        "intent_type":  intent_type,
        "nlu_feedback": result.get("nlu_feedback", "好的，我来处理。"),
    }
    if intent_type == "define_rule":
        base["rule"] = result.get("rule", {})
    else:
        base["requirements"] = result.get("requirements", [])
    return base
