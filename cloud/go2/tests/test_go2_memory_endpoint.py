"""Go2 成长资产只读端点。"""
import json
import httpx
import pytest
from fastapi import FastAPI

from cloud.go2 import router as go2_router
from cloud.go2.agentcore.tools import tools


@pytest.mark.asyncio
async def test_memory_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "test-service-token")
    f = tmp_path / "rules.json"
    f.write_text(json.dumps([{"id": "tb_1", "trigger": "人", "action": "Hello",
                              "behavior": "打招呼", "cooldown_s": 30,
                              "enabled": True, "hit_count": 2,
                              "last_fired_ts": 1.0, "last_triggered": 0}]),
                 encoding="utf-8")
    monkeypatch.setattr(tools, "RULES_FILE", f)

    app = FastAPI()
    app.include_router(go2_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.get("/api/go2/memory", headers={
            "X-SSM-Service-Token": "test-service-token",
            "X-SSM-Session-Id": "test-admin",
            "X-SSM-Actor": "admin",
            "X-SSM-Persist": "true",
        })
    assert r.status_code == 200
    body = r.json()
    assert len(body["taught"]) == 1
    assert body["taught"][0]["hit_count"] == 2
    assert "today" in body
    assert isinstance(body["today"], str)
