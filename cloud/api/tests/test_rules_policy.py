from fastapi import FastAPI
import httpx
import pytest

from cloud.api import rules
from cloud.api.rules import router as rules_router
from cloud.api.sessions import router as sessions_router

from .conftest import login_admin


@pytest.mark.asyncio
async def test_rules_write_is_admin_only(api_env, monkeypatch, tmp_path):
    monkeypatch.setattr(rules, "_RULES_FILE", tmp_path / "rules.json")
    monkeypatch.setattr(rules, "_push_rules_to_esp32", lambda: None)
    app = FastAPI()
    app.include_router(sessions_router)
    app.include_router(rules_router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin:
        csrf = await login_admin(admin)
        created = await admin.post(
            "/api/rules",
            json={"name": "r", "trigger": {}, "action": {}, "enabled": True},
            headers=csrf,
        )
        assert created.status_code == 200

        token = (await admin.post("/api/admin/invite/rotate", headers=csrf)).json()["token"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as visitor:
            assert (await visitor.post("/api/visitor/session", json={"token": token})).status_code == 200
            visitor_csrf = {"X-CSRF-Token": visitor.cookies.get("ssm_csrf")}
            denied = await visitor.post(
                "/api/rules", json={"name": "bad", "trigger": {}, "action": {}}, headers=visitor_csrf,
            )
            assert denied.status_code == 403
            assert len(rules._load_rules()) == 1
