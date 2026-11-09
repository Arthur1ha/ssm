"""Page conversations stay isolated while the signed login survives reload."""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from cloud.api import auth, intents
from cloud.agents.common.context import thread_id_for


@pytest.fixture
def login(monkeypatch):
    monkeypatch.setattr(intents, 'schedule_session_cleanup', lambda session: None)
    return auth.new_session('admin')


async def context(login, chat_id=None, *, events=False):
    headers = [(b'cookie', f'{auth.SESSION_COOKIE}={auth.encode_session(login)}'.encode())]
    if chat_id is not None and not events:
        headers.append((b'x-ssm-chat-id', chat_id.encode()))
    request = Request({'type': 'http', 'method': 'GET', 'scheme': 'http', 'server': ('test', 80),
                       'path': '/api/events' if events else '/api/me', 'headers': headers,
                       'query_string': f'chat_id={chat_id}'.encode() if events and chat_id is not None else b''})
    return await auth.require_session(request)


async def test_refresh_changes_context_without_changing_login(login):
    before = await context(login, 'a' * 32)
    after = await context(login, 'b' * 32)
    assert before['session_id'] != after['session_id']
    assert before['login_session_id'] == after['login_session_id'] == login['session_id']
    assert before['actor'] == after['actor'] == login['actor']
    assert before['csrf'] == after['csrf'] == login['csrf']
    assert before['expires_at'] == after['expires_at'] == login['expires_at']
    assert auth.public_session(before)['session_id'] == login['session_id']
    assert auth.decode_session(auth.encode_session(login)) == login
    for agent in ('cloud_orchestrator', 'esp32_desk_led', 'go2'):
        assert thread_id_for(agent, before['session_id']) != thread_id_for(agent, after['session_id'])


async def test_reconnect_uses_same_scope_and_no_previous_page_replay(login):
    before = await context(login, 'a' * 32)
    reconnect = await context(login, 'a' * 32, events=True)
    after = await context(login, 'b' * 32, events=True)
    assert before == reconnect
    broker = intents._EventBroker()
    broker.emit(before['session_id'], {'event_id': 'old-reply', 'type': 'agent_reply'})
    assert broker.subscribe(reconnect).get_nowait()['event_id'] == 'old-reply'
    assert broker.subscribe(after).empty()
    broker.emit(before['session_id'], {'event_id': 'late-old-reply', 'type': 'agent_reply'})
    assert broker.subscribe(after).empty()


async def test_nonce_cannot_address_another_login(login):
    first = await context(login, 'a' * 32)
    other = await context(auth.new_session('admin'), 'a' * 32)
    assert first['session_id'] != other['session_id']


@pytest.mark.parametrize('nonce', ['', '../other', 'a' * 31, 'a' * 33])
async def test_invalid_nonce_is_rejected(login, nonce):
    with pytest.raises(HTTPException) as error:
        await context(login, nonce)
    assert error.value.status_code == 400


async def test_legacy_clients_without_nonce_keep_authenticated_scope(login):
    assert await context(login) == login


async def test_gateway_passes_page_scope_to_all_three_agents(login, monkeypatch):
    from fastapi import FastAPI
    import httpx
    from cloud.api import activity
    from cloud.agents.common.checkpoint import FileCheckpointSaver

    published, devices = [], []

    async def intervention(*args):
        return 1

    async def lamp_chat(unit_id, message, session, **kwargs):
        devices.append(('lamp', session['session_id']))
        return {'result': 'ok', 'reply': '模拟回复'}

    async def go2_chat(action, params, session, **kwargs):
        devices.append(('go2', session['session_id']))
        return {'result': 'ok', 'response': '模拟回复'}

    monkeypatch.setattr(activity, 'user_intervention', intervention)
    monkeypatch.setattr(FileCheckpointSaver, 'get_tuple', lambda *args: None)
    monkeypatch.setattr(intents, '_mqtt_publish_signed', lambda topic, payload: published.append((topic, payload)))
    monkeypatch.setattr(intents, 'emit_agent_event', lambda *args, **kwargs: None)
    monkeypatch.setattr(intents, 'execute_lamp_chat', lamp_chat)
    monkeypatch.setattr(intents, '_dispatch_go2', go2_chat)
    app = FastAPI()
    app.include_router(intents.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        client.cookies.set(auth.SESSION_COOKIE, auth.encode_session(login))
        client.cookies.set(auth.CSRF_COOKIE, login['csrf'])
        for nonce in ('a' * 32, 'b' * 32):
            headers = {'X-SSM-Chat-Id': nonce, auth.CSRF_HEADER: login['csrf']}
            result = await client.post('/api/intents', json={'text': '模拟指令'}, headers=headers)
            assert result.status_code == 202
            sid = result.json()['session_id']
            assert sid != login['session_id']
            assert published[-1][0] == f'ssm/intent/{sid}'
            assert published[-1][1]['context']['session_id'] == sid
            for unit_id in ('esp32_desk_led', 'go2'):
                result = await client.post(f'/api/commands/{unit_id}', headers=headers,
                                     json={'action': 'chat', 'params': {'message': '模拟设备对话'}})
                assert result.status_code == 200
                assert devices[-1][1] == sid
        assert devices[0][1] != devices[2][1]
        assert (await client.post('/api/intents', headers={'X-SSM-Chat-Id': 'a' * 32},
                           json={'text': '缺少 CSRF'})).status_code == 403
