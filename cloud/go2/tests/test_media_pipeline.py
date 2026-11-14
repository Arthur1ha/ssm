"""Offline regressions for media latency, freshness and connection retirement."""
import asyncio
import io
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from PIL import Image

from cloud.go2.connection import Go2Connection
from cloud.go2.connection import video


def camera():
    result = video.Go2Video()
    result.is_connected = True
    result._odom_session_id = 'camera-a'
    result._odom = {'x': 1, 'y': 2}
    return result


def install_frame(robot, *, age=0, session='camera-a', number=1):
    robot._latest_frame = b'original'
    robot._latest_preview_frame = b'preview'
    robot._latest_frame_meta = {'frame_id': number, 'received_at': time.monotonic() - age,
                                'captured_at': time.time() - age, 'odom_session_id': session}
    robot._video_track_ended = False


@pytest.mark.asyncio
async def test_slow_encoding_drains_camera_and_keeps_only_latest_pending_frame(monkeypatch):
    robot = camera()
    incoming = asyncio.Queue()
    started = threading.Event()
    release = threading.Event()
    encoded = []
    def encode(frame):
        encoded.append(frame)
        if frame == 1:
            started.set()
            assert release.wait(2)
        return str(frame).encode(), b'preview', 1280, 720
    monkeypatch.setattr(video, '_encode_frame', encode)
    robot._video_task = asyncio.create_task(robot._consume_video(SimpleNamespace(recv=incoming.get)))
    try:
        await incoming.put(1)
        while not started.is_set():
            await asyncio.sleep(.001)
        for frame in range(2, 61):
            incoming.put_nowait(frame)
        for _ in range(500):
            if incoming.empty():
                break
            await asyncio.sleep(.001)
        assert incoming.empty(), 'camera receive must continue while JPEG encoding is blocked'
        assert robot._video_dropped_frames >= 58
        release.set()
        for _ in range(500):
            if robot._latest_frame == b'60':
                break
            await asyncio.sleep(.001)
        assert encoded == [1, 60]
        assert robot._latest_frame == b'60'
        assert robot._latest_frame_meta['pose_at_capture'] == {'x': 1, 'y': 2}
    finally:
        release.set()
        await robot._stop_video()


def test_native_evidence_and_smaller_shared_preview_keep_separate_dimensions():
    image = Image.new('RGB', (1280, 720), (10, 90, 120))
    original, preview, width, height = video._encode_frame(SimpleNamespace(to_image=lambda: image))
    assert (width, height) == (1280, 720)
    assert Image.open(io.BytesIO(original)).size == (1280, 720)
    assert Image.open(io.BytesIO(preview)).size == (640, 360)
    assert len(preview) < len(original)


@pytest.mark.asyncio
async def test_stream_has_lengths_skips_stale_frames_and_stops_at_session_change():
    robot = camera()
    install_frame(robot)
    stream = robot.mjpeg_generator()
    part = await anext(stream)
    header, jpeg = part.split(b'\r\n\r\n', 1)
    assert b'Content-Length: 7' in header
    assert b'X-Frame-Id: 1' in header
    assert b'X-Frame-Age-Ms:' in header
    assert jpeg == b'preview\r\n'
    install_frame(robot, age=video.VIDEO_MAX_AGE_S + 1, number=2)
    assert robot.live_frame_snapshot() is None
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    install_frame(robot)
    stream = robot.mjpeg_generator()
    await anext(stream)
    robot._odom_session_id = 'camera-b'
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert robot.live_frame_snapshot() is None
    assert robot.latest_frame_snapshot()[0] == b'original'  # raw evidence has its own consumers


@pytest.mark.asyncio
async def test_old_video_stop_cannot_clear_a_new_session():
    robot = camera()
    entered = asyncio.Event()
    release = asyncio.Event()
    async def old_task():
        try:
            await asyncio.Event().wait()
        finally:
            entered.set()
            await release.wait()
    robot._video_task = asyncio.create_task(old_task())
    await asyncio.sleep(0)
    cleanup = asyncio.create_task(robot._stop_video())
    await entered.wait()
    robot._odom_session_id = 'camera-b'
    install_frame(robot, session='camera-b')
    release.set()
    await cleanup
    assert robot._latest_frame == b'original'
    assert robot._video_track_ended is False


@pytest.mark.asyncio
async def test_video_snapshot_rejects_expired_or_ended_media(monkeypatch):
    from cloud.go2 import router
    robot = camera()
    monkeypatch.setattr(router, 'go2', robot)
    install_frame(robot)
    response = await router.go2_video_snapshot()
    assert response.body == b'original'
    preview = await router.go2_video_snapshot(preview=True)
    assert preview.body == b'preview'
    assert preview.headers['x-frame-id'] == '1'
    assert preview.headers['x-accel-buffering'] == 'no'
    robot._video_track_ended = True
    with pytest.raises(HTTPException) as ended:
        await router.go2_video_snapshot()
    assert ended.value.status_code == 503
    install_frame(robot, age=video.VIDEO_MAX_AGE_S + 1)
    with pytest.raises(HTTPException):
        await router.go2_video_snapshot()


@pytest.mark.asyncio
async def test_map_render_is_shared_and_does_not_block_the_event_loop(monkeypatch):
    from cloud.go2 import router
    from cloud.go2.navigation import map_image
    from cloud.agents.go2.memory import spatial
    started, release = threading.Event(), threading.Event()
    calls = []
    grid = SimpleNamespace(observed_at=time.monotonic(), session_id='map-a', frame_id='odom')
    robot = SimpleNamespace(is_connected=True, _odom_session_id='map-a', occupancy_grid=grid,
                            voxel_received_at=time.monotonic(), odom={}, odom_received_at=None,
                            global_path=[], odom_frame_id='odom')
    def render(*args):
        calls.append(args)
        started.set()
        assert release.wait(2)
        return b'PNG'
    monkeypatch.setattr(router, 'go2', robot)
    monkeypatch.setattr(router, '_map_image_task', None)
    monkeypatch.setattr(map_image, 'render_map_png', render)
    monkeypatch.setattr(spatial, 'list_locations', lambda: [])
    first = asyncio.create_task(router.go2_map_image())
    try:
        while not started.is_set():
            await asyncio.sleep(.001)
        second = asyncio.create_task(router.go2_map_image())
        await asyncio.sleep(.01)
        assert not first.done() and not second.done()
        assert len(calls) == 1
        release.set()
        a, b = await asyncio.gather(first, second)
        assert a.body == b.body == b'PNG'
        grid.observed_at = time.monotonic() - 21
        with pytest.raises(HTTPException) as expired:
            await router.go2_map_image()
        assert expired.value.status_code == 503
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_reconnect_during_map_render_rejects_retired_image(monkeypatch):
    from cloud.go2 import router
    from cloud.go2.navigation import map_image
    from cloud.agents.go2.memory import spatial
    started, release = threading.Event(), threading.Event()
    grid = SimpleNamespace(observed_at=time.monotonic(), session_id='map-a', frame_id='odom')
    robot = SimpleNamespace(is_connected=True, _odom_session_id='map-a', occupancy_grid=grid,
                            voxel_received_at=time.monotonic(), odom={}, odom_received_at=None,
                            global_path=[], odom_frame_id='odom')
    def render(*args):
        started.set()
        assert release.wait(2)
        return b'OLD PNG'
    monkeypatch.setattr(router, 'go2', robot)
    monkeypatch.setattr(router, '_map_image_task', None)
    monkeypatch.setattr(map_image, 'render_map_png', render)
    monkeypatch.setattr(spatial, 'list_locations', lambda: [])
    request = asyncio.create_task(router.go2_map_image())
    try:
        while not started.is_set():
            await asyncio.sleep(.001)
        robot._odom_session_id = 'map-b'
        release.set()
        with pytest.raises(HTTPException) as retired:
            await request
        assert retired.value.status_code == 503
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_constructor_failure_and_cancelled_handshake_leave_offline_and_close_peer(monkeypatch):
    from cloud.go2.connection import webrtc
    robot = Go2Connection()
    monkeypatch.setattr(webrtc, 'UnitreeWebRTCConnection', MagicMock(side_effect=RuntimeError('auth failed')))
    with pytest.raises(RuntimeError, match='auth failed'):
        await robot.connect('test', 'test', 'test')
    assert robot.runtime.data['connection_state'] == 'offline'
    entered = asyncio.Event()
    async def handshake():
        entered.set()
        await asyncio.Event().wait()
    connection = SimpleNamespace(connect=handshake, disconnect=AsyncMock())
    monkeypatch.setattr(webrtc, 'UnitreeWebRTCConnection', lambda *args, **kwargs: connection)
    connecting = asyncio.create_task(robot.connect('test', 'test', 'test'))
    await entered.wait()
    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    connection.disconnect.assert_awaited_once()
    assert robot._conn is None
    assert robot.runtime.data['connection_state'] == 'offline'


@pytest.mark.asyncio
async def test_late_disconnect_cleanup_does_not_close_new_peer():
    robot = Go2Connection()
    started, release = asyncio.Event(), asyncio.Event()
    async def close_old():
        started.set()
        await release.wait()
    old = SimpleNamespace(disconnect=close_old)
    robot._conn = old
    robot.is_connected = True
    retiring = asyncio.create_task(robot.disconnect())
    await started.wait()
    new = SimpleNamespace(disconnect=AsyncMock())
    robot._conn = new
    robot._odom_session_id = 'new-session'
    robot.is_connected = True
    install_frame(robot, session='new-session')
    release.set()
    await retiring
    new.disconnect.assert_not_awaited()
    assert robot._conn is new and robot.is_connected
    assert robot._latest_frame == b'original'


@pytest.mark.asyncio
async def test_cancelled_close_keeps_real_aiortc_close_alive(monkeypatch):
    from aiortc import RTCPeerConnection
    peer = RTCPeerConnection()
    peer.createDataChannel('test')
    entered, release = asyncio.Event(), asyncio.Event()
    stop = peer.sctp.stop
    async def slow_stop():
        entered.set()
        await release.wait()
        await stop()
    monkeypatch.setattr(peer.sctp, 'stop', slow_stop)
    connection = SimpleNamespace(disconnect=peer.close)
    closing = asyncio.create_task(Go2Connection._close_connection(connection))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        closing.cancel()
        await asyncio.sleep(0)
        assert not connection._ssm_close_task.cancelled()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        # aiortc's second close waits on its first internal completion future.
        # This hangs if cancellation was allowed through to the actual close.
        await asyncio.wait_for(peer.close(), .5)
        assert peer.connectionState == 'closed'
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)
        await asyncio.wait_for(peer.close(), .5)


@pytest.mark.asyncio
async def test_cancelled_connect_while_previous_peer_closes_returns_offline(monkeypatch):
    from cloud.go2.connection import webrtc
    robot = Go2Connection()
    entered, release = asyncio.Event(), asyncio.Event()
    async def close_previous():
        entered.set()
        await release.wait()
    previous = SimpleNamespace(disconnect=AsyncMock(side_effect=close_previous))
    robot._conn = previous
    robot.is_connected = True
    construct = MagicMock()
    monkeypatch.setattr(webrtc, 'UnitreeWebRTCConnection', construct)
    connecting = asyncio.create_task(robot.connect('test', 'test', 'test'))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        connecting.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        assert robot.runtime.data['connection_state'] == 'offline'
        assert robot._conn is None
        previous.disconnect.assert_awaited_once()
        construct.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)


@pytest.mark.asyncio
async def test_retired_encoder_error_cannot_replace_new_session_diagnostics(monkeypatch):
    robot = camera()
    incoming = asyncio.Queue()
    entered, release = threading.Event(), threading.Event()
    def encode(frame):
        entered.set()
        assert release.wait(2)
        raise ValueError('retired frame')
    monkeypatch.setattr(video, '_encode_frame', encode)
    consuming = asyncio.create_task(robot._consume_video(SimpleNamespace(recv=incoming.get)))
    try:
        incoming.put_nowait(1)
        for _ in range(500):
            if entered.is_set():
                break
            await asyncio.sleep(.001)
        assert entered.is_set()
        robot._odom_session_id = 'camera-b'
        install_frame(robot, session='camera-b')
        robot._video_error = 'new-session-diagnostic'
        release.set()
        await asyncio.wait_for(consuming, 1)
        assert robot._video_error == 'new-session-diagnostic'
        assert not robot._video_track_ended
        assert robot._latest_frame == b'original'
    finally:
        release.set()
        consuming.cancel()
        await asyncio.gather(consuming, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_rpc_uses_real_sdk_resolver_identity_and_drops_only_its_future():
    from unitree_webrtc_connect.msgs.pub_sub import WebRTCDataChannelPubSub
    from unitree_webrtc_connect.constants import DATA_CHANNEL_TYPE, RTC_TOPIC
    robot = Go2Connection()
    sent = []
    pub_sub = WebRTCDataChannelPubSub(SimpleNamespace(readyState='open', send=lambda raw: sent.append(json.loads(raw))))
    resolver = pub_sub.future_resolver
    unrelated = asyncio.get_running_loop().create_future()
    resolver.save_resolve(DATA_CHANNEL_TYPE['REQUEST'], RTC_TOPIC['SPORT_MOD'], unrelated, 999)
    connection = SimpleNamespace(datachannel=SimpleNamespace(pub_sub=pub_sub))
    request = asyncio.create_task(robot._publish_sport_request(connection, {'api_id': 1004, 'id': 123}))
    await asyncio.sleep(0)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    resolver.run_resolve_for_topic({'type': DATA_CHANNEL_TYPE['RESPONSE'], 'topic': RTC_TOPIC['SPORT_MOD'],
                                   'data': {'header': {'identity': {'id': 123}}}})
    assert resolver.pending_callbacks == {999: [unrelated]}
    assert not unrelated.done()


@pytest.mark.asyncio
async def test_remote_auth_runs_off_loop_and_late_result_cannot_create_peer(monkeypatch):
    from cloud.go2.connection import remote_signaling as remote
    from unitree_webrtc_connect import WebRTCConnectionMethod
    started, release = threading.Event(), threading.Event()
    owner = threading.get_ident()
    def prepare(*args):
        assert threading.get_ident() != owner
        started.set()
        assert release.wait(2)
        return 'token', 'key', {'relay': 'simulated'}
    monkeypatch.setattr(remote, '_prepare', prepare)
    monkeypatch.setattr(remote, '_SIGNALING_TIMEOUT_S', .03)
    connection = remote.RemoteConnection(WebRTCConnectionMethod.Remote, username='test', password='test', serialNumber='test')
    connection.init_webrtc = AsyncMock()
    task = asyncio.create_task(connection.connect())
    try:
        while not started.is_set():
            await asyncio.sleep(.001)
        with pytest.raises(asyncio.TimeoutError):
            await task
        release.set()
        await asyncio.sleep(.02)
        connection.init_webrtc.assert_not_awaited()
        assert connection.pc is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_timed_out_signaling_holds_its_slot_without_queuing_retry(monkeypatch):
    from cloud.go2.connection import remote_signaling as remote
    monkeypatch.setattr(remote, '_SIGNALING_LOCK', threading.Lock())
    monkeypatch.setattr(remote, '_SIGNALING_TIMEOUT_S', .03)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def blocked():
        entered.set()
        try:
            assert release.wait(2)
            return 'late'
        finally:
            finished.set()
    initial = asyncio.create_task(remote._cloud_io(blocked))
    try:
        for _ in range(500):
            if entered.is_set():
                break
            await asyncio.sleep(.001)
        assert entered.is_set()
        with pytest.raises(asyncio.TimeoutError):
            await initial
        assert remote._SIGNALING_LOCK.locked()
        monkeypatch.setattr(remote, '_SIGNALING_TIMEOUT_S', 1)
        retried = []
        with pytest.raises(RuntimeError, match='still in progress'):
            await remote._cloud_io(lambda: retried.append(True))
        assert retried == []
        release.set()
        for _ in range(500):
            if finished.is_set() and not remote._SIGNALING_LOCK.locked():
                break
            await asyncio.sleep(.001)
        assert not remote._SIGNALING_LOCK.locked()
        assert await remote._cloud_io(lambda: 'next') == 'next'
    finally:
        release.set()
        await asyncio.gather(initial, return_exceptions=True)
