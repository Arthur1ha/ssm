# mqtt_client.py — MQTT transport that never retries inside a hardware tick.

import time
import ujson as json
import network
import socket
from umqtt.simple import MQTTClient as _MQTTClient
from config import (MQTT_BROKER_IP, MQTT_PORT, DEVICE_ID, MQTT_USER,
                    MQTT_PASSWORD, WIFI_SSID, WIFI_PASSWORD)

_STATUS_TOPIC = "ssm/agents/{}/status".format(DEVICE_ID)
PING_INTERVAL_MS = 30000
RETRY_INTERVAL_MS = 5000
SOCKET_TIMEOUT_S = 3


class MqttClient:
    def __init__(self):
        self._subs = []
        self._callback = None
        self._client = None
        self._last_ping = 0
        self._last_attempt = None
        self._on_reconnect = None
        self._wlan = network.WLAN(network.STA_IF)

    def begin(self):
        # Initial broker failure must not prevent the LED tick loop from starting.
        self._try_connect()

    def set_callback(self, cb):
        def _wrapper(topic, payload):
            t = topic.decode()
            try:
                p = json.loads(payload)
            except Exception:
                p = payload.decode()
            cb(t, p)
        self._callback = _wrapper
        if self._client:
            self._client.set_callback(_wrapper)

    def set_reconnect_callback(self, cb):
        self._on_reconnect = cb

    def subscribe(self, topic):
        if topic not in self._subs:
            self._subs.append(topic)
        if self._client:
            try:
                self._client.sock.settimeout(SOCKET_TIMEOUT_S)
                self._client.subscribe(topic)
            except Exception as exc:
                self._drop(exc)

    def publish(self, topic, payload, retain=False, qos=0):
        if self._client is None:
            return False
        if isinstance(payload, dict):
            data = json.dumps(payload).encode()
        elif isinstance(payload, str):
            data = payload.encode()
        else:
            data = payload
        try:
            self._client.sock.settimeout(SOCKET_TIMEOUT_S)
            self._client.publish(topic, data, retain=retain, qos=qos)
            return True
        except Exception as exc:
            self._drop(exc)
            return False

    def check_msg(self):
        """Poll once; disconnected retries are spaced out across main loop ticks."""
        if not self._wlan.isconnected():
            self._drop("WiFi disconnected")
            self._retry_wifi()
            return
        if self._client is None:
            self._try_connect()
            return
        now = time.ticks_ms()
        try:
            if time.ticks_diff(now, self._last_ping) >= PING_INTERVAL_MS:
                self._client.sock.settimeout(SOCKET_TIMEOUT_S)
                self._client.ping()
                self._last_ping = now
            self._client.check_msg()
        except Exception as exc:
            self._drop(exc)

    def _retry_due(self):
        now = time.ticks_ms()
        if (self._last_attempt is not None and
                time.ticks_diff(now, self._last_attempt) < RETRY_INTERVAL_MS):
            return False
        self._last_attempt = now
        return True

    def _retry_wifi(self):
        if not self._retry_due():
            return
        try:
            self._wlan.active(True)
            self._wlan.connect(WIFI_SSID, WIFI_PASSWORD)
            print("[WiFi] Reconnecting...")
        except Exception as exc:
            print("[WiFi] Reconnect failed: {}".format(exc))

    def _try_connect(self):
        if not self._retry_due():
            return
        if not self._wlan.isconnected():
            self._last_attempt = None
            self._retry_wifi()
            return
        client = _MQTTClient(
            client_id=DEVICE_ID, server=MQTT_BROKER_IP, port=MQTT_PORT,
            keepalive=60, user=MQTT_USER, password=MQTT_PASSWORD,
        )
        client.set_last_will(_STATUS_TOPIC, b"offline", retain=True, qos=1)
        if self._callback:
            client.set_callback(self._callback)
        try:
            try:
                client.connect(timeout=SOCKET_TIMEOUT_S)
            except TypeError:
                # Older umqtt.simple versions do not accept a timeout keyword.
                # Probe TCP with a deadline before their unbounded connect().
                probe = socket.socket()
                try:
                    probe.settimeout(SOCKET_TIMEOUT_S)
                    probe.connect((MQTT_BROKER_IP, MQTT_PORT))
                finally:
                    probe.close()
                client.connect()
            for topic in self._subs:
                client.sock.settimeout(SOCKET_TIMEOUT_S)
                client.subscribe(topic)
        except Exception as exc:
            self._close(client)
            print("[MQTT] Connect failed: {}".format(exc))
            return
        self._client = client
        self._last_ping = time.ticks_ms()
        if not self.publish(_STATUS_TOPIC, "online", retain=True):
            return
        print("[MQTT] Connected to {}:{}".format(MQTT_BROKER_IP, MQTT_PORT))
        if self._on_reconnect:
            self._on_reconnect()

    def _drop(self, reason):
        if self._client is None:
            return
        client = self._client
        self._client = None
        self._close(client)
        print("[MQTT] Disconnected: {}".format(reason))

    @staticmethod
    def _close(client):
        try:
            if client.sock:
                client.sock.close()
        except Exception:
            pass
