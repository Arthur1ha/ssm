"""Keep SDK cloud signaling off the event loop; WebRTC stays on its owner loop.

Only synchronous cloud helpers run in a daemon worker. Their results cannot
install a peer or change runtime state after cancellation. An unfinished SDK
HTTP call keeps the single-flight lock until it really finishes; retries fail
explicitly instead of queuing more calls or blocking process exit.
"""
import asyncio
import json
import threading

from cloud.agents.common.runner import invoke_blocking

from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.util import fetch_token, fetch_public_key, fetch_turn_server_info
from unitree_webrtc_connect.unitree_auth import send_sdp_to_remote_peer

_SIGNALING_LOCK = threading.Lock()
_SIGNALING_TIMEOUT_S = 15.0


async def _cloud_io(callback, *args, **kwargs):
    def run():
        if not _SIGNALING_LOCK.acquire(blocking=False):
            raise RuntimeError('Go2 cloud signaling is still in progress; retry after it finishes')
        try:
            return callback(*args, **kwargs)
        finally:
            _SIGNALING_LOCK.release()

    return await asyncio.wait_for(invoke_blocking(run), timeout=_SIGNALING_TIMEOUT_S)


def _prepare(username, password, serial, region, device_type):
    token = fetch_token(username, password, region=region, device_type=device_type)
    if not token:
        raise RuntimeError('Go2 cloud authentication failed')
    key = fetch_public_key(region=region, device_type=device_type)
    relay = fetch_turn_server_info(serial, token, key, region=region, device_type=device_type)
    return token, key, relay


class RemoteConnection(UnitreeWebRTCConnection):
    """Small adapter for the SDK's synchronous Remote auth and SDP exchange."""

    def __init__(self, method, *, username=None, password=None, **kwargs):
        # The SDK constructor otherwise performs synchronous HTTP login.
        self._login = (username, password)
        super().__init__(method, username=None, password=None, **kwargs)

    async def connect(self):
        if self.connectionMethod != WebRTCConnectionMethod.Remote:
            return await super().connect()
        token, key, relay = await _cloud_io(
            _prepare, *self._login, self.sn, self.region, self.device_type)
        self.token, self.public_key = token, key
        await self.init_webrtc(relay)

    async def get_answer_from_remote_peer(self, pc, turn_server_info):
        offer = pc.localDescription
        payload = json.dumps({'id': '', 'turnserver': turn_server_info,
                              'sdp': offer.sdp, 'type': offer.type, 'token': self.token})
        return await _cloud_io(send_sdp_to_remote_peer, self.sn, payload, self.token,
                               self.public_key, region=self.region, device_type=self.device_type)
