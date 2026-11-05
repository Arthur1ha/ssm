"""Regression checks for releasing Go2 activity motion ownership."""

from types import SimpleNamespace

import pytest

from cloud.go2 import router


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_result,release_expected", [
    ({"result": "accepted", "request_status_code": 0}, True),
    ({"result": "accepted", "request_status_code": None}, True),
    ({"result": "error", "request_status_code": 1}, False),
])
async def test_end_activity_releases_accepted_stop_lease(
        monkeypatch, stop_result, release_expected):
    calls = []

    async def send_command(command, *, completion_timeout):
        calls.append((command, completion_timeout))
        return stop_result

    monkeypatch.setattr(router, "_activity_mode_owner", "act_previous")
    monkeypatch.setattr(router, "_autonomy_mode", "free_explore")
    monkeypatch.setattr(router, "_configure_vision_consumers",
                        lambda mode: calls.append(("mode", mode)))
    monkeypatch.setattr(router, "go2", SimpleNamespace(
        is_connected=True,
        stop_velocity=lambda: calls.append(("velocity", 0)),
        send_command=send_command))
    monkeypatch.setattr(router, "control_lease", SimpleNamespace(
        release=lambda owner: calls.append(("release", owner))))
    from cloud.peers import gateway
    monkeypatch.setattr(gateway, "cancel_pending", lambda **_kwargs: None)

    result = await router.set_activity_mode("manual", "act_previous")

    assert result["ok"] is True
    assert result["physical_stop"] == "unknown"
    assert calls[:3] == [("mode", "manual"), ("velocity", 0), ("StopMove", 5.0)]
    assert (("release", "act_previous") in calls) is release_expected
