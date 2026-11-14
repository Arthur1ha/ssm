"""SSE stays responsive to MQTT threads and disconnects without worker waits."""

import asyncio
import threading
import time

import pytest
from starlette.requests import Request

from cloud.api import intents


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
