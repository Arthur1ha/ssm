import os, json
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

client = OpenAI(
    base_url="https://tokenhub.tencentmaas.com/v1",
    api_key=os.getenv("CHAT_API_KEY", "sk-nWxTcBZGRyI7B5JB0XWJvl7bukVIRudanTWuiXS4UNiA2dse"),
)
MODEL = "hy3-preview"

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

class ChatRequest(BaseModel):
    message: str
    devices: list

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
