import os, json, uuid, time as _time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

client = OpenAI(
    base_url=os.getenv("CHAT_API_BASE_URL", "https://tokenhub.tencentmaas.com/v1"),
    api_key=os.getenv("CHAT_API_KEY"),
)
MODEL = os.getenv("CHAT_MODEL", "hy3-preview")

SYSTEM = """你是 SSM 智能家居助手。用自然语言简短回复用户（1-2句话），同时调用 mqtt_publish 工具发送必要的设备控制指令。
- 如果用户想控制设备，先回复确认，再发指令
- 如果设备不支持该操作，说明原因
- 如果只是问问题，只回复不发指令"""

TOOLS = [{
    "type": "function",
    "function": {
        "name": "mqtt_publish",
        "description": "向设备发送 MQTT 控制指令",
        "parameters": {
            "type": "object",
            "properties": {
                "topic":   {"type": "string", "description": "MQTT topic，例如 ssm/agents/esp32_desk_led/command"},
                "payload": {"type": "object", "description": "指令内容，例如 {\"cmd\": \"SET_COLOR\", \"r\": 255, \"g\": 0, \"b\": 0, \"brightness\": 200}"}
            },
            "required": ["topic", "payload"]
        }
    }
}]

NLU_SYSTEM = """你是 SSM 智能家居语音助手的意图解析器。将用户输入解析为结构化 JSON。

输出格式（只输出 JSON，不要代码块和解释）：
{
  "nlu_feedback": "明白了，你想看书，我来帮你调亮一点。",
  "requirements": [
    {"resource_tag": "lighting", "action": "brighten", "context": "reading"}
  ]
}

resource_tag 选择：lighting（灯光）、ambiance（氛围）、alert（警报）、notification（通知）
action 选择：set_color、brighten、dim、off、on、blink、alert、notify
nlu_feedback 要求：自然口语，1-2句，提前预告要做什么"""


class ChatRequest(BaseModel):
    message: str
    devices: list


class NLURequest(BaseModel):
    message: str
    devices: list = []

@app.post("/api/chat")
def chat(req: ChatRequest):
    actuators = [d for d in req.devices if d.get("agent_type") == "actuator" and d.get("topics", {}).get("command")]
    device_ctx = json.dumps(actuators, ensure_ascii=False, indent=2)

    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=512,
        tools=TOOLS,
        tool_choice="auto",
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user",   "content": f"当前在线可控设备：\n{device_ctx}\n\n用户说：{req.message}"}
        ]
    )

    msg = response.choices[0].message
    reply = msg.content or "好的，指令已发送"
    commands = []
    if msg.tool_calls:
        for tc in msg.tool_calls:
            if tc.function.name == "mqtt_publish":
                commands.append(json.loads(tc.function.arguments))

    return {"reply": reply, "commands": commands}


@app.post("/api/nlu")
def nlu(req: NLURequest):
    session_id = f"s_{int(_time.time())}_{uuid.uuid4().hex[:6]}"

    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=256,
        messages=[
            {"role": "system", "content": NLU_SYSTEM},
            {"role": "user",   "content": req.message},
        ]
    )

    content = response.choices[0].message.content.strip()
    try:
        result = json.loads(content)
    except Exception:
        result = {"nlu_feedback": "好的，我来帮你处理。", "requirements": []}

    return {
        "session_id":   session_id,
        "nlu_feedback": result.get("nlu_feedback", "好的，我来处理。"),
        "requirements": result.get("requirements", []),
    }
