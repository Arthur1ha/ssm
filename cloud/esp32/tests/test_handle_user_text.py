"""灯智能体设备页对话入口：分类闲聊/调教并落库。"""
import json
from cloud.esp32.agent import ESP32Agent
from cloud.esp32.state import ESP32State
from cloud.esp32.memory import taught


class _StubLLM:
    """返回预设 JSON 字符串的假 LLM。"""
    def __init__(self, payload: dict):
        self._payload = payload

    def invoke(self, _messages):
        class _Resp:
            def __init__(self, payload):
                self.content = json.dumps(payload, ensure_ascii=False)
        return _Resp(self._payload)


def test_teach_message_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(taught, "TAUGHT_FILE", tmp_path / "taught.json")
    agent = ESP32Agent(ESP32State(), llm=_StubLLM({
        "type": "teach", "trigger": "天黑了", "behavior": "调亮一点",
        "reply": "学会了：天黑就调亮"}))

    reply = agent.handle_user_text("以后天黑了你就调亮一点")

    assert "学会" in reply
    rules = taught.list_all()
    assert len(rules) == 1
    assert rules[0]["trigger"] == "天黑了"


def test_chat_message_does_not_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(taught, "TAUGHT_FILE", tmp_path / "taught.json")
    agent = ESP32Agent(ESP32State(), llm=_StubLLM({
        "type": "chat", "reply": "你好呀~"}))

    reply = agent.handle_user_text("你好")

    assert reply == "你好呀~"
    assert taught.list_all() == []


def test_llm_parse_error_degrades_gracefully(tmp_path, monkeypatch):
    monkeypatch.setattr(taught, "TAUGHT_FILE", tmp_path / "taught.json")

    class _BrokenLLM:
        def invoke(self, _messages):
            class _Resp:
                content = "not json at all"
            return _Resp()

    agent = ESP32Agent(ESP32State(), llm=_BrokenLLM())
    reply = agent.handle_user_text("随便说点什么")
    assert "没太听明白" in reply
    assert taught.list_all() == []
