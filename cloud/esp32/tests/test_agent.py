import time
import pytest
from unittest.mock import MagicMock, patch
from langchain_core.messages import AIMessage
from cloud.esp32.agent import ESP32Agent
from cloud.esp32.state import ESP32State


def make_agent(llm=None):
    state = ESP32State()
    return ESP32Agent(state, llm=llm or MagicMock())


def make_agent_with_snapshot(sensor_snapshot, actuator_snapshot=None):
    mock_state = MagicMock(spec=ESP32State)
    mock_state.sensor_snapshot.return_value = sensor_snapshot
    mock_state.actuator_snapshot.return_value = actuator_snapshot or {}
    return ESP32Agent(mock_state, llm=MagicMock())


class TestSkeleton:
    def test_instantiates(self):
        assert make_agent() is not None

    def test_has_start_method(self):
        assert callable(make_agent().start)

    def test_has_push_sensor_event(self):
        assert callable(make_agent().push_sensor_event)

    def test_belief_history_starts_empty(self):
        assert make_agent()._belief_history == []

    def test_cooldown_starts_empty(self):
        assert make_agent()._cooldown == {}

    def test_push_sensor_event_puts_to_queue(self):
        agent = make_agent()
        agent.push_sensor_event("esp32_desk_light", {"level": "DARK"})
        assert agent._event_queue.qsize() == 1


class TestSense:
    def test_returns_none_when_no_light_data(self):
        agent = make_agent_with_snapshot({})
        assert agent._sense() is None

    def test_returns_light_level_dark(self):
        snap = {"esp32_desk_light": {"state": {"level": "DARK", "lux": 50, "ts": 1000}}}
        agent = make_agent_with_snapshot(snap)
        result = agent._sense()
        assert result is not None
        assert result["light_level"] == "DARK"
        assert result["light_lux"] == 50

    def test_falls_back_to_event_when_no_state(self):
        snap = {"esp32_desk_light": {"event": {"level": "DIM", "lux": 120, "ts": 1000}}}
        agent = make_agent_with_snapshot(snap)
        assert agent._sense()["light_level"] == "DIM"

    def test_sound_recent_true_within_5s(self):
        now = time.time()
        snap = {
            "esp32_desk_light": {"state": {"level": "NORMAL", "lux": 300, "ts": now}},
            "esp32_desk_sound": {"event": {"ts": now - 2}},
        }
        agent = make_agent_with_snapshot(snap)
        assert agent._sense()["sound_recent"] is True

    def test_sound_recent_false_older_than_5s(self):
        now = time.time()
        snap = {
            "esp32_desk_light": {"state": {"level": "NORMAL", "lux": 300, "ts": now}},
            "esp32_desk_sound": {"event": {"ts": now - 10}},
        }
        agent = make_agent_with_snapshot(snap)
        assert agent._sense()["sound_recent"] is False

    def test_no_sound_sensor(self):
        snap = {"esp32_desk_light": {"state": {"level": "NORMAL", "lux": 300, "ts": 1000}}}
        agent = make_agent_with_snapshot(snap)
        result = agent._sense()
        assert result["sound_detected"] is False
        assert result["sound_recent"] is False

    def test_no_context_combo_in_result(self):
        snap = {"esp32_desk_light": {"state": {"level": "DARK", "lux": 10, "ts": 1000}}}
        agent = make_agent_with_snapshot(snap)
        result = agent._sense()
        assert "context_combo" not in result

    def test_includes_led_device_id(self):
        snap = {"esp32_desk_light": {"state": {"level": "NORMAL", "lux": 300, "ts": 1000}}}
        mock_state = MagicMock(spec=ESP32State)
        mock_state.sensor_snapshot.return_value = snap
        mock_state.actuator_snapshot.return_value = {"esp32_desk_led": {"state": {"ism": "BRIGHT"}}}
        agent = ESP32Agent(mock_state, llm=MagicMock())
        result = agent._sense()
        assert result["led_device_id"] == "esp32_desk_led"


class TestReason:
    SENSE_DATA = {
        "light_level": "DARK", "light_value": 100, "light_lux": 30,
        "sound_detected": False, "sound_recent": False,
        "led_state": "OFF", "led_device_id": "esp32_desk_led",
        "time_str": "20:00", "time_period": "夜间",
    }

    def _make_with_llm_response(self, content: str):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content=content)
        return ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)

    def test_returns_list_of_tool_calls(self):
        agent = self._make_with_llm_response(
            '[{"tool": "set_led_color", "params": {"r": 255, "g": 160, "b": 60, "brightness": 160}}]'
        )
        result = agent._reason(self.SENSE_DATA)
        assert isinstance(result, list)
        assert result[0]["tool"] == "set_led_color"

    def test_returns_empty_list_for_no_action(self):
        agent = self._make_with_llm_response("[]")
        result = agent._reason(self.SENSE_DATA)
        assert result == []

    def test_returns_none_on_invalid_json(self):
        agent = self._make_with_llm_response("不是 JSON")
        assert agent._reason(self.SENSE_DATA) is None

    def test_handles_json_with_preamble(self):
        agent = self._make_with_llm_response(
            '好的，结果如下：\n[{"tool": "speak", "params": {"text": "光线不足"}}]'
        )
        result = agent._reason(self.SENSE_DATA)
        assert result is not None
        assert result[0]["tool"] == "speak"

    def test_prompt_includes_raw_sense_values(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content="[]")
        agent = ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)
        agent._reason(self.SENSE_DATA)
        prompt = mock_llm.invoke.call_args[0][0][0].content
        assert "DARK" in prompt
        assert "20:00" in prompt

    def test_prompt_includes_tool_descriptions(self):
        from cloud.esp32.tools import TOOL_DESCRIPTIONS
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content="[]")
        agent = ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)
        agent._reason(self.SENSE_DATA)
        prompt = mock_llm.invoke.call_args[0][0][0].content
        assert "set_led_state" in prompt or "set_led_color" in prompt

    def test_prompt_includes_history(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content="[]")
        agent = ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)
        agent._belief_history = [
            {"context": "光线昏暗有人活动", "actions": ["set_led_color"], "ts": time.time() - 120},
        ]
        agent._reason(self.SENSE_DATA)
        prompt = mock_llm.invoke.call_args[0][0][0].content
        assert "光线昏暗有人活动" in prompt

    def test_prompt_does_not_contain_explicit_rules(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content="[]")
        agent = ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)
        agent._reason(self.SENSE_DATA)
        prompt = mock_llm.invoke.call_args[0][0][0].content
        # 不应出现硬编码规则关键字
        assert "dark_active" not in prompt
        assert "→ state_action" not in prompt
        assert "决策规则（按优先级）" not in prompt


class TestAct:
    def _make_agent(self):
        mock_state = MagicMock(spec=ESP32State)
        mock_state.actuator_snapshot.return_value = {
            "esp32_desk_led": {"state": {"ism": "BRIGHT"}}
        }
        return ESP32Agent(mock_state, llm=MagicMock())

    def _belief_state(self, state="BRIGHT", color=None):
        return {
            "state_action": {"state": state},
            "color_action": color,
        }

    def _belief_color(self, r=255, g=200, b=100, brightness=180):
        return {
            "state_action": None,
            "color_action": {"r": r, "g": g, "b": b, "brightness": brightness},
        }

    def test_publishes_set_state_bright(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.publish_task") as mock_pub:
            agent._act(self._belief_state("BRIGHT"))
            mock_pub.assert_called_once()
            assert mock_pub.call_args[0][2] == "SET_STATE"

    def test_bright_with_color_uses_set_color(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.publish_task") as mock_pub:
            agent._act({"state_action": {"state": "BRIGHT"}, "color_action": {"r": 255, "g": 100, "b": 50, "brightness": 180}})
            assert mock_pub.call_args[0][2] == "SET_COLOR"

    def test_off_ignores_color(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.publish_task") as mock_pub:
            agent._act({"state_action": {"state": "OFF"}, "color_action": {"r": 255, "g": 0, "b": 0, "brightness": 255}})
            assert mock_pub.call_args[0][2] == "SET_STATE"
            assert mock_pub.call_args[0][3] == {"state": "OFF"}

    def test_null_actions_publish_nothing(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.publish_task") as mock_pub:
            agent._act({"state_action": None, "color_action": None})
            mock_pub.assert_not_called()

    def test_cooldown_blocks_duplicate(self):
        agent = self._make_agent()
        belief = self._belief_state("BRIGHT")
        with patch("cloud.esp32.tools.publish_task") as mock_pub:
            agent._act(belief)
            agent._act(belief)
            assert mock_pub.call_count == 1

    def test_cooldown_allows_different_state(self):
        agent = self._make_agent()
        with patch("cloud.esp32.tools.publish_task") as mock_pub:
            agent._act(self._belief_state("BRIGHT"))
            agent._act(self._belief_state("OFF"))
            assert mock_pub.call_count == 2


class TestBeliefHistory:
    def test_reason_returns_list(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content="[]")
        agent = ESP32Agent(MagicMock(spec=ESP32State), llm=mock_llm)
        sense = {
            "light_level": "NORMAL", "light_value": 300, "light_lux": 300,
            "sound_detected": False, "sound_recent": False,
            "led_state": "OFF", "led_device_id": "esp32_desk_led",
            "time_str": "10:00", "time_period": "上午",
        }
        result = agent._reason(sense)
        assert isinstance(result, list)


class TestRunIntent:
    def _make_agent_with_llm(self, content: str):
        from unittest.mock import AsyncMock
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content=content))
        state = MagicMock(spec=ESP32State)
        state.get_manifest.return_value = {
            "capabilities": ["SET_STATE", "SET_COLOR"],
            "resource_tags": ["lighting"],
        }
        return ESP32Agent(state, llm=mock_llm)

    @pytest.mark.asyncio
    async def test_returns_task_ids(self):
        agent = self._make_agent_with_llm(
            '[{"device_id": "esp32_desk_led", "action": "SET_COLOR", "params": {"r": 255, "g": 200, "b": 100, "brightness": 180}}]'
        )
        with patch("cloud.esp32.tools.publish_task"):
            result = await agent.run_intent("s1", "暖色调", ["esp32_desk_led"])
        assert result["status"] == "dispatched"
        assert len(result["task_ids"]) == 1
        assert result["task_ids"][0] == "s1_t0"

    @pytest.mark.asyncio
    async def test_publishes_correct_mqtt_command(self):
        agent = self._make_agent_with_llm(
            '[{"device_id": "esp32_desk_led", "action": "SET_STATE", "params": {"state": "OFF"}}]'
        )
        with patch("cloud.esp32.tools.publish_task") as mock_pub:
            await agent.run_intent("s1", "关灯", ["esp32_desk_led"])
        mock_pub.assert_called_once()
        args = mock_pub.call_args[0]
        assert args[0] == "esp32_desk_led"
        assert args[2] == "SET_STATE"
        assert args[3] == {"state": "OFF"}

    @pytest.mark.asyncio
    async def test_returns_empty_on_invalid_llm_response(self):
        agent = self._make_agent_with_llm("not valid json")
        with patch("cloud.esp32.tools.publish_task"):
            result = await agent.run_intent("s1", "开灯", ["esp32_desk_led"])
        assert result["task_ids"] == []
        assert result["status"] == "dispatched"

    @pytest.mark.asyncio
    async def test_skips_malformed_commands(self):
        agent = self._make_agent_with_llm(
            '[{"device_id": "esp32_desk_led"}, {"device_id": "esp32_desk_led", "action": "SET_STATE", "params": {}}]'
        )
        with patch("cloud.esp32.tools.publish_task") as mock_pub:
            result = await agent.run_intent("s1", "开灯", ["esp32_desk_led"])
        assert len(result["task_ids"]) == 1
        mock_pub.assert_called_once()


from fastapi.testclient import TestClient
from fastapi import FastAPI
from cloud.esp32.router import router as esp32_router
import cloud.esp32.agent as agent_mod


class TestRouter:
    def setup_method(self):
        from unittest.mock import AsyncMock
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=AIMessage(
            content='[{"device_id": "esp32_desk_led", "action": "SET_STATE", "params": {"state": "BRIGHT"}}]'
        ))
        mock_state = MagicMock(spec=ESP32State)
        mock_state.get_manifest.return_value = {"capabilities": ["SET_STATE"]}
        agent_mod._agent = ESP32Agent(mock_state, llm=mock_llm)

        app = FastAPI()
        app.include_router(esp32_router)
        self.client = TestClient(app)

    def test_run_returns_200(self):
        with patch("cloud.esp32.tools.publish_task"):
            resp = self.client.post("/api/esp32/run", json={
                "session_id": "test_s1",
                "goal": "开灯",
                "device_ids": ["esp32_desk_led"],
            })
        assert resp.status_code == 200

    def test_run_returns_task_ids(self):
        with patch("cloud.esp32.tools.publish_task"):
            resp = self.client.post("/api/esp32/run", json={
                "session_id": "test_s1",
                "goal": "开灯",
                "device_ids": ["esp32_desk_led"],
            })
        data = resp.json()
        assert "task_ids" in data
        assert data["status"] == "dispatched"

    def test_run_returns_503_when_no_agent(self):
        agent_mod._agent = None
        resp = self.client.post("/api/esp32/run", json={
            "session_id": "s1", "goal": "开灯", "device_ids": []
        })
        assert resp.status_code == 503
