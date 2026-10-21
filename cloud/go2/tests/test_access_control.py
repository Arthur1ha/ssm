import asyncio
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from cloud.go2.control import ControlLease, LeaseConflict
from cloud.go2.execution import ExecutionContext, require_service_context, use_context


VISITOR_A = ExecutionContext("visitor-a", "visitor", False)
VISITOR_B = ExecutionContext("visitor-b", "visitor", False)
ADMIN = ExecutionContext("admin-a", "admin", True)


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def test_internal_token_and_role_make_visitor_non_persistent(monkeypatch):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "secret")
    context = asyncio.run(require_service_context(
        x_ssm_service_token="secret",
        x_ssm_session_id="ses-v",
        x_ssm_actor="visitor",
        x_ssm_persist="true",  # forged escalation is ignored
        x_ssm_space_id="main",
    ))
    assert context == ExecutionContext("ses-v", "visitor", False)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_service_context(
            x_ssm_service_token="wrong",
            x_ssm_session_id="ses-v",
            x_ssm_actor="visitor",
            x_ssm_persist="false",
            x_ssm_space_id="main",
        ))
    assert exc.value.status_code == 401


def test_visitor_final_write_guards_leave_every_file_unchanged(tmp_path, monkeypatch):
    from cloud.go2.agentcore.memory import spatial
    from cloud.go2.agentcore.memory.episode import EpisodeMemory, EventType
    from cloud.go2.agentcore.memory.people import KnownPerson, PeopleMemory
    from cloud.go2.agentcore.memory import daily_summary
    from cloud.go2.agentcore import soul
    from cloud.go2.agentcore.tools import tools

    spatial_file = tmp_path / "memory" / "spatial.json"
    rules_file = tmp_path / "rules.json"
    personality_file = tmp_path / "soul" / "personality.json"
    traits_file = tmp_path / "soul" / "traits.json"
    people_file = tmp_path / "memory" / "people.json"
    episodes_dir = tmp_path / "memory" / "episodes"
    summaries_dir = tmp_path / "memory" / "summaries"
    for path, content in (
        (spatial_file, "{}"), (rules_file, "[]"),
        (personality_file, '{"prompt":"original"}'),
        (traits_file, '{"curiosity":70}'),
        (people_file, '{"version":1,"people":{}}'),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    episode = EpisodeMemory(episodes_dir=episodes_dir)
    monkeypatch.setattr(spatial, "SPATIAL_FILE", spatial_file)
    monkeypatch.setattr(tools, "RULES_FILE", rules_file)
    monkeypatch.setattr(soul, "_PERSONALITY_FILE", personality_file)
    monkeypatch.setattr(soul, "_TRAITS_FILE", traits_file)

    before = _snapshot(tmp_path)
    with use_context(VISITOR_A):
        assert episode.add(EventType.USER_COMMAND, "visitor text") is False
        for operation in (
            lambda: spatial.tag_location("home", {"x": 1, "y": 2, "heading": 0}),
            lambda: spatial.delete_location("home"),
            lambda: tools.save_rules([{"trigger": "x"}]),
            lambda: soul.set_personality("forged"),
            lambda: soul._save_traits({"curiosity": 0}, traits_file),
            lambda: PeopleMemory(people_file).upsert(KnownPerson(
                "p1", "visitor", "known", "test", "g", 1, 1, 1,
            )),
        ):
            with pytest.raises(PermissionError):
                operation()
        with pytest.raises(PermissionError):
            asyncio.run(daily_summary.generate_and_save(
                "2026-10-19", episodes_dir=episodes_dir, base_dir=summaries_dir,
            ))

    assert _snapshot(tmp_path) == before


def test_identity_remote_write_is_blocked_before_provider_call(tmp_path):
    from cloud.go2.agentcore.memory.people import PeopleMemory
    from cloud.go2.agentcore.skills.identity import IdentitySkill

    provider = MagicMock()
    provider.create_person = AsyncMock()
    skill = IdentitySkill(
        provider, PeopleMemory(tmp_path / "people.json"), lambda: (None, None),
        enrollment_enabled=True,
    )
    with use_context(VISITOR_A), pytest.raises(PermissionError):
        asyncio.run(skill.remember_person("someone", "known"))
    provider.create_person.assert_not_awaited()
    assert not (tmp_path / "people.json").exists()


def test_global_identity_vision_uses_non_persistent_context(tmp_path, monkeypatch):
    from cloud.go2 import router as router_mod
    from cloud.go2.agentcore.memory.episode import EpisodeMemory
    from cloud.go2.agentcore.skills.identity import IdentityEvent, IdentitySnapshot
    from cloud.go2.execution import current_context

    seen = []
    monkeypatch.setattr(
        router_mod.identity_skill, "on_vision_frame",
        lambda _frame: seen.append(current_context()),
    )
    router_mod._on_identity_vision_frame({"faces": {"count": 1}})
    assert seen == [router_mod._IDENTITY_VISION_CONTEXT]
    assert seen[0].actor == "visitor"
    assert seen[0].persist is False

    memory = EpisodeMemory(episodes_dir=tmp_path / "episodes")
    monkeypatch.setattr(router_mod, "episode_memory", memory)
    event = IdentityEvent(
        IdentitySnapshot("idle", ts=1),
        IdentitySnapshot("recognized", "p1", "访客", "known", 99, 2),
    )
    with use_context(router_mod._IDENTITY_VISION_CONTEXT):
        router_mod._on_identity_event(event)
    assert memory.entries() == []
    assert not tuple((tmp_path / "episodes").glob("*.jsonl"))

    # Only an explicit trusted admin request (never ambient SYSTEM_CONTEXT)
    # may turn the same callback into a long-term episode.
    with use_context(ADMIN):
        router_mod._on_identity_event(event)
    assert len(memory.entries()) == 1
    assert memory.entries()[0]["event_type"] == "IDENTITY_CHANGE"


def test_visitor_cannot_read_long_term_assets_through_tools():
    from cloud.go2.agentcore.tools import tools

    with use_context(VISITOR_A):
        with pytest.raises(PermissionError):
            tools.go2_list_people()


def test_control_lease_conflict_renewal_expiry_and_admin_preemption():
    lease = ControlLease(ttl_s=10)
    first = lease.acquire(VISITOR_A, now=100)
    assert first.owner_session_id == "visitor-a"
    renewed = lease.heartbeat(VISITOR_A, now=105)
    assert renewed.expires_at == 115
    with pytest.raises(LeaseConflict):
        lease.acquire(VISITOR_B, now=106)
    preempted = lease.acquire(ADMIN, now=107)
    assert preempted.owner_session_id == "admin-a"

    expiring = ControlLease(ttl_s=2)
    expiring.acquire(VISITOR_A, now=100)
    assert expiring.acquire(VISITOR_B, now=103).owner_session_id == "visitor-b"


def test_go2_short_term_history_is_isolated_and_cleanup_is_precise():
    from cloud.go2 import agent

    agent.cleanup_session("visitor-a")
    agent.cleanup_session("visitor-b")
    agent._remember_turn("visitor-a", "用户", "A secret")
    agent._remember_turn("visitor-b", "用户", "B secret")
    assert "A secret" in agent._history_context("visitor-a")
    assert "B secret" not in agent._history_context("visitor-a")
    assert "B secret" in agent._history_context("visitor-b")

    assert agent.cleanup_session("visitor-a") is True
    assert "A secret" not in agent._history_context("visitor-a")
    assert "B secret" in agent._history_context("visitor-b")
    assert agent.cleanup_session("visitor-a") is False  # idempotent
    agent.cleanup_session("visitor-b")


@pytest.mark.asyncio
async def test_browser_session_can_read_but_cannot_bypass_command_gateway(
    monkeypatch, tmp_path,
):
    from cloud.api import auth
    from cloud.go2.router import router
    from cloud.space import registry as space_registry

    monkeypatch.setenv("SSM_SESSION_SECRET", "s" * 48)
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "i" * 32)
    store = space_registry.SpaceRegistry(tmp_path / "space.json")
    monkeypatch.setattr(space_registry, "_registry", store)

    app = FastAPI()
    app.include_router(router)
    admin = auth.new_session("admin")
    cookies = {auth.SESSION_COOKIE: auth.encode_session(admin)}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        cookies=cookies,
    ) as client:
        assert (await client.get("/api/go2/connection")).status_code == 200
        # Device writes are internal-only. A browser admin must use
        # /api/commands/{unit_id}, where policy and auditing are enforced.
        response = await client.post("/api/go2/commands", json={"cmd": "StandUp"})
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_visitor_cannot_read_long_term_go2_assets(monkeypatch, tmp_path):
    from cloud.api import auth
    from cloud.go2.router import router
    from cloud.space import registry as space_registry

    monkeypatch.setenv("SSM_SESSION_SECRET", "s" * 48)
    store = space_registry.SpaceRegistry(tmp_path / "space.json")
    monkeypatch.setattr(space_registry, "_registry", store)
    invite = store.enable_invite()
    visitor = auth.new_session("visitor", invite_version=invite["version"])

    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        cookies={auth.SESSION_COOKIE: auth.encode_session(visitor)},
    ) as client:
        assert (await client.get("/api/go2/identity")).status_code == 200
        assert (await client.get("/api/go2/memory")).status_code == 403
        assert (await client.get("/api/go2/people")).status_code == 403
        assert (await client.get("/api/go2/personality")).status_code == 403


@pytest.mark.asyncio
async def test_session_cleanup_cancels_timed_chat_move_and_stops_robot(monkeypatch):
    from cloud.go2 import agent, router
    from cloud.go2.agentcore.tools import tools
    from cloud.go2.connection import go2

    move_started = asyncio.Event()
    commands = []

    async def fake_send_command(command, params=None):
        commands.append((command, params))
        if command == "Move":
            move_started.set()
        return {"result": "accepted"}

    async def fake_run_agent(session_id, message, *, context, **_kwargs):
        with use_context(context):
            return await tools.go2_move("forward", duration=10)

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    monkeypatch.setattr(go2, "send_command", fake_send_command)
    zero_velocity = MagicMock(return_value={"result": "ok"})
    monkeypatch.setattr(go2, "move_velocity", zero_velocity)
    go2.is_connected = True
    router.control_lease.release(VISITOR_A.session_id)

    request_task = asyncio.create_task(
        router.go2_chat(router.ChatRequest(message="向前走十秒"), context=VISITOR_A)
    )
    try:
        await asyncio.wait_for(move_started.wait(), timeout=1)
        cleanup = router.cleanup_session_resources(VISITOR_A.session_id)
        with pytest.raises(asyncio.CancelledError):
            await request_task

        assert cleanup["tasks_cancelled"] == 1
        assert cleanup["motion_stopped"] is True
        assert commands[0][0] == "Move"
        assert commands[-1][0] == "StopMove"
        zero_velocity.assert_any_call(0, 0, 0)
        assert router.control_lease.snapshot() is None
    finally:
        if not request_task.done():
            request_task.cancel()
        router.cleanup_session_resources(VISITOR_A.session_id)
        go2.is_connected = False

