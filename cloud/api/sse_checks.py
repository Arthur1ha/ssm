"""SSE stays responsive to MQTT threads and disconnects without worker waits."""

import asyncio
import json
import threading
import time

import pytest
from starlette.requests import Request

from cloud.api import intents


@pytest.mark.parametrize('stage', [
    'mapping_proposal', 'terms_proposal', 'agreement', 'terms_confirmed',
    'identification', 'recognition', 'reveal', 'rps_result', 'settlement',
    'search_observe',
])
async def test_activity_stage_keeps_activity_sse_name_and_matches_history(monkeypatch, tmp_path, stage):
    from cloud.api import activity as activity_api
    from cloud.space.activity import ActivityStore

    store = ActivityStore(tmp_path / 'activity.json')
    revision = store.note_user_intervention('request-game')
    record = store.begin('玩石头剪刀布', 'request-game', revision)
    monkeypatch.setattr(activity_api, 'get_activity_store', lambda: store)
    broker, stream = await open_stream(monkeypatch)
    store.configure(emit=lambda event: broker.emit_space(event['space_id'], event))
    try:
        game_round = {'round': 1, 'choices': {'esp32_desk_led': 'scissors', 'go2': 'rock'},
            'mapping': {
                'esp32_desk_led': {'choice': 'scissors', 'color': {'r': 0, 'g': 255, 'b': 0}, 'label': '绿色'},
                'go2': {'choice': 'rock', 'action': 'go2_sport', 'arguments': {'cmd': 'Hello'}, 'label': '打招呼'}},
            'winner': 'go2'}
        event = store.record_event(record['activity_id'], 'message', unit_id='go2',
            game_id='rps_siege', stage=stage, text='石头＝开心；剪刀＝比心；布＝伸展。', game_round=game_round)
        wire = await asyncio.wait_for(anext(stream), timeout=1)
        assert '\nevent: activity\n' in wire
        payload = json.loads(next(line[6:] for line in wire.splitlines() if line.startswith('data: ')))
        assert payload == event
        history = await activity_api.get_activity_events(record['activity_id'], {'space_id': 'main'})
        assert history['events'][-1] == payload
        assert payload['stage'] == stage
        if stage == 'rps_result':
            assert payload['game_round'] == game_round
        else:
            assert 'game_round' not in payload
    finally:
        await stream.aclose()


def test_legacy_feedback_still_routes_by_stage():
    assert intents._sse_event_name({'stage': 'planning'}) == 'planning'
    assert intents._sse_event_name({'type': 'agent_progress', 'stage': 'search_observe'}) == 'agent_progress'


async def open_stream(monkeypatch):
    broker = intents._EventBroker()
    monkeypatch.setattr(intents, '_events', broker)
    session = {'session_id': 'sse-probe', 'actor': 'admin', 'space_id': 'main',
               'expires_at': time.time() + 60}
    request = Request({'type': 'http', 'headers': []})
    response = await intents.session_events(request, session)
    assert 'event: ready' in await anext(response.body_iterator)
    return broker, response.body_iterator


async def test_sse_delivers_thread_events_in_order_and_unsubscribes(monkeypatch):
    broker, stream = await open_stream(monkeypatch)
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)

    def publish():
        for number in range(2):
            broker.emit('sse-probe', {'type': 'agent_reply', 'event_id': f'event-{number}'})

    worker = threading.Thread(target=publish)
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert 'id: event-0\n' in await asyncio.wait_for(pending, timeout=1)
    assert 'id: event-1\n' in await anext(stream)
    await stream.aclose()
    assert 'sse-probe' not in broker._queues


async def test_idle_sse_cancellation_does_not_leave_a_worker(monkeypatch):
    def unexpected_worker(*args, **kwargs):
        raise AssertionError('An idle SSE reader must not occupy a worker thread')

    monkeypatch.setattr(intents.asyncio, 'to_thread', unexpected_worker)
    broker, stream = await open_stream(monkeypatch)
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert 'sse-probe' not in broker._queues


async def test_heartbeat_timeout_keeps_later_thread_events():
    broker = intents._EventBroker()
    channel = broker.subscribe('sse-probe')
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(channel.get_async(), timeout=0.01)

    pending = asyncio.create_task(channel.get_async())
    await asyncio.sleep(0)
    worker = threading.Thread(target=broker.emit, args=(
        'sse-probe', {'event_id': 'after-heartbeat', 'type': 'agent_reply'}))
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert (await asyncio.wait_for(pending, timeout=1))['event_id'] == 'after-heartbeat'
    broker.unsubscribe('sse-probe', channel)
