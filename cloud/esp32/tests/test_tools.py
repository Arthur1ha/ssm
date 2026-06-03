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
