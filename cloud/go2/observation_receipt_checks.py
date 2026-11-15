"""A lamp's confirmed color must be observed on a later camera frame."""
import base64
import time
from types import SimpleNamespace

import pytest

from cloud.go2.perception import visual_target
from cloud.go2.tools import executor, tools
from cloud.space.execution import ExecutionContext


@pytest.fixture
def observer(monkeypatch):
    boundary = time.monotonic()
    frames = iter([
        (b"old-color", {"frame_id": 1, "received_at": boundary - .1,
                        "captured_at": time.time(), "odom_session_id": "session"}),
        (b"new-color", {"frame_id": 2, "received_at": boundary + .001,
                        "captured_at": time.time(), "odom_session_id": "session"}),
    ])
    robot = SimpleNamespace(is_connected=True, _odom_session_id="session",
                            latest_frame_snapshot=lambda: next(frames))
    monkeypatch.setattr(executor, "go2", robot)
    monkeypatch.setattr(executor, "runtime_for", lambda _robot: None)
    monkeypatch.setattr(executor, "_audit", lambda _name, _context, result, *_args: result)
    monkeypatch.setattr(tools, "go2", robot)
    monkeypatch.setattr(visual_target, "go2", robot)
    monkeypatch.setattr(visual_target, "image_quality", lambda _jpeg: 1.0)
    images = []

    class Model:
        async def ainvoke(self, messages):
            images.append(messages[1].content[1]["image_url"]["url"])
            return SimpleNamespace(content="桌上有一盏亮蓝色的圆球灯。")

    monkeypatch.setattr(tools, "get_vision_llm", Model)
    context = ExecutionContext("game", "visitor", False, origin_kind="agent_autonomous",
                               expires_at=int(time.time()) + 60)
    return boundary, context, images


async def test_game_observation_skips_frames_from_before_color_receipt(observer):
    boundary, context, images = observer
    result = await executor.execute_tool("go2_observe", {"question": "找蓝色圆球灯"},
        context=context, observation_after=boundary)
    assert result["result"] == "ok" and "圆球灯" in result["message"]
    assert images == ["data:image/jpeg;base64," + base64.b64encode(b"new-color").decode("ascii")]


async def test_model_cannot_supply_internal_observation_boundary(observer):
    boundary, context, images = observer
    result = await executor.execute_tool("go2_observe", {
        "question": "找灯", "after_received_at": boundary}, context=context)
    assert result["result"] == "rejected" and result["error"] == "invalid_params"
    assert not images
