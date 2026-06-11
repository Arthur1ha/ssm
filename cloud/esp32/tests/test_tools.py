import json
from unittest.mock import MagicMock, patch
from cloud.esp32 import tools


def make_mock_mqtt():
    mqtt = MagicMock()
    tools.init(mqtt)
    return mqtt


class TestPublishTask:
    def test_publishes_to_correct_topic(self):
        mqtt = make_mock_mqtt()
        tools.publish_task("esp32_desk_led", "t1", "SET_STATE", {"state": "BRIGHT"}, "s1")
        topic = mqtt.publish.call_args[0][0]
        assert topic == "ssm/task/esp32_desk_led/t1"

    def test_payload_contains_required_fields(self):
        mqtt = make_mock_mqtt()
        tools.publish_task("esp32_desk_led", "t1", "SET_COLOR", {"r": 255}, "s1")
        payload = json.loads(mqtt.publish.call_args[0][1])
        assert payload["task_id"] == "t1"
        assert payload["session_id"] == "s1"
        assert payload["action"] == "SET_COLOR"
        assert payload["params"] == {"r": 255}
        assert "ts" in payload

    def test_publishes_with_qos_1(self):
        mqtt = make_mock_mqtt()
        tools.publish_task("esp32_desk_led", "t1", "SET_STATE", {}, "s1")
        assert mqtt.publish.call_args[1].get("qos") == 1


class TestPublishLedMood:
    def test_publishes_to_led_mood_topic(self):
        mqtt = make_mock_mqtt()
        tools.publish_led_mood("thinking")
        topic = mqtt.publish.call_args[0][0]
        assert topic == "ssm/agents/desk/led_mood"

    def test_payload_contains_mood(self):
        mqtt = make_mock_mqtt()
        tools.publish_led_mood("idle")
        payload = json.loads(mqtt.publish.call_args[0][1])
        assert payload["mood"] == "idle"


class TestPublishThought:
    def test_publishes_to_thought_topic(self):
        mqtt = make_mock_mqtt()
        tools.publish_thought("interesting pattern")
        topic = mqtt.publish.call_args[0][0]
        assert topic == "ssm/agents/desk/thought"

    def test_payload_contains_text(self):
        mqtt = make_mock_mqtt()
        tools.publish_thought("hello")
        payload = json.loads(mqtt.publish.call_args[0][1])
        assert payload["text"] == "hello"


class TestPublishSpeech:
    def test_publishes_to_speech_topic(self):
        mqtt = make_mock_mqtt()
        with patch("cloud.esp32.tts.synthesize", return_value=None):
            tools.publish_speech("你好")
        topic = mqtt.publish.call_args[0][0]
        assert topic == "ssm/agents/desk/speech"

    def test_payload_includes_audio_when_tts_succeeds(self):
        mqtt = make_mock_mqtt()
        with patch("cloud.esp32.tts.synthesize", return_value="base64audio"):
            tools.publish_speech("你好")
        payload = json.loads(mqtt.publish.call_args[0][1])
        assert payload["audio"] == "base64audio"
        assert payload["text"] == "你好"

    def test_payload_has_no_audio_key_when_tts_fails(self):
        mqtt = make_mock_mqtt()
        with patch("cloud.esp32.tts.synthesize", return_value=None):
            tools.publish_speech("你好")
        payload = json.loads(mqtt.publish.call_args[0][1])
        assert "audio" not in payload


class TestSetLedState:
    def test_publishes_set_state_action(self):
        mqtt = make_mock_mqtt()
        tools.set_led_state("esp32_desk_led", "BRIGHT")
        payload = json.loads(mqtt.publish.call_args[0][1])
        assert payload["action"] == "SET_STATE"
        assert payload["params"]["state"] == "BRIGHT"

    def test_correct_topic(self):
        mqtt = make_mock_mqtt()
        tools.set_led_state("esp32_desk_led", "OFF")
        topic = mqtt.publish.call_args[0][0]
        assert topic.startswith("ssm/task/esp32_desk_led/")

    def test_returns_string(self):
        make_mock_mqtt()
        result = tools.set_led_state("esp32_desk_led", "DIM")
        assert isinstance(result, str)


class TestSetLedColor:
    def test_publishes_set_color_action(self):
        mqtt = make_mock_mqtt()
        tools.set_led_color("esp32_desk_led", 255, 160, 60, 160)
        payload = json.loads(mqtt.publish.call_args[0][1])
        assert payload["action"] == "SET_COLOR"
        assert payload["params"] == {"r": 255, "g": 160, "b": 60, "brightness": 160}

    def test_correct_topic(self):
        mqtt = make_mock_mqtt()
        tools.set_led_color("esp32_desk_led", 0, 0, 0, 0)
        topic = mqtt.publish.call_args[0][0]
        assert topic.startswith("ssm/task/esp32_desk_led/")

    def test_returns_string(self):
        make_mock_mqtt()
        result = tools.set_led_color("esp32_desk_led", 255, 200, 100, 180)
        assert isinstance(result, str)


class TestSpeakTool:
    def test_calls_publish_speech_internally(self):
        make_mock_mqtt()
        with patch("cloud.esp32.tts.synthesize", return_value=None):
            result = tools.speak("灯已打开")
        assert isinstance(result, str)

    def test_speak_also_publishes_thought(self):
        mqtt = make_mock_mqtt()
        with patch("cloud.esp32.tts.synthesize", return_value=None):
            tools.speak("灯已调暖色")
        topics = [call[0][0] for call in mqtt.publish.call_args_list]
        assert "ssm/agents/desk/thought" in topics

    def test_speak_thought_payload_contains_text(self):
        mqtt = make_mock_mqtt()
        with patch("cloud.esp32.tts.synthesize", return_value=None):
            tools.speak("行吧，给你暖了")
        thought_calls = [c for c in mqtt.publish.call_args_list
                         if c[0][0] == "ssm/agents/desk/thought"]
        assert len(thought_calls) == 1
        payload = json.loads(thought_calls[0][0][1])
        assert payload["text"] == "行吧，给你暖了"


class TestToolFnMap:
    def test_all_tools_present(self):
        assert "set_led_state" in tools.TOOL_FN_MAP
        assert "set_led_color" in tools.TOOL_FN_MAP
        assert "speak" in tools.TOOL_FN_MAP

    def test_all_callables(self):
        for name, fn in tools.TOOL_FN_MAP.items():
            assert callable(fn), f"{name} 应为可调用对象"

    def test_tool_descriptions_is_string(self):
        assert isinstance(tools.TOOL_DESCRIPTIONS, str)
        assert "set_led_state" in tools.TOOL_DESCRIPTIONS
        assert "set_led_color" in tools.TOOL_DESCRIPTIONS
        assert "speak" in tools.TOOL_DESCRIPTIONS
