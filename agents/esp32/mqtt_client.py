# mqtt_client.py — Non-blocking MQTT wrapper using umqtt.robust
# Fixes: keepalive ping + reconnect re-publish callback

import time
import json
from umqtt.robust import MQTTClient as _MQTTClient
from config import MQTT_BROKER_IP, MQTT_PORT, AGENT_ID, MQTT_USER, MQTT_PASSWORD

_STATUS_TOPIC = "ssm/agents/{}/status".format(AGENT_ID)

PING_INTERVAL_MS = 30000   # send PING every 30s (keepalive=60, broker timeout=90s)


class MqttClient:
    def __init__(self):
        self._subs          = []
        self._callback      = None
        self._client        = None
        self._last_ping     = 0
        self._on_reconnect  = None   # callback: called after successful reconnect

    def begin(self):
        self._client = _MQTTClient(
            client_id = AGENT_ID,
            server    = MQTT_BROKER_IP,
            port      = MQTT_PORT,
            keepalive = 60,
            user      = MQTT_USER,
            password  = MQTT_PASSWORD,
        )
        self._client.set_last_will(
            topic  = _STATUS_TOPIC,
            msg    = b"offline",
            retain = True,
            qos    = 1,
        )
        if self._callback:
            self._client.set_callback(self._callback)

        self._client.connect()
        self._last_ping = time.ticks_ms()
        print("[MQTT] Connected to {}:{}".format(MQTT_BROKER_IP, MQTT_PORT))

        self.publish(_STATUS_TOPIC, "online", retain=True)

        for topic in self._subs:
            self._client.subscribe(topic)

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
        """cb() is called after a successful reconnect, so app can re-publish manifest etc."""
        self._on_reconnect = cb

    def subscribe(self, topic):
        if topic not in self._subs:
            self._subs.append(topic)
        if self._client:
            self._client.subscribe(topic)

    def publish(self, topic, payload, retain=False, qos=0):
        if self._client is None:
            return
        if isinstance(payload, dict):
            data = json.dumps(payload).encode()
        elif isinstance(payload, str):
            data = payload.encode()
        else:
            data = payload
        try:
            self._client.publish(topic, data, retain=retain, qos=qos)
        except Exception as e:
            print("[MQTT] publish error: {}".format(e))

    def check_msg(self):
        """Non-blocking check + keepalive ping. Call every main loop iteration."""
        if self._client is None:
            return

        now = time.ticks_ms()

        # ── Send keepalive PING every 30s ──────────────────────
        if time.ticks_diff(now, self._last_ping) >= PING_INTERVAL_MS:
            self._last_ping = now
            try:
                self._client.ping()
            except Exception:
                pass   # umqtt.robust will handle reconnect on next operation

        # ── Check for incoming messages ─────────────────────────
        try:
            self._client.check_msg()
        except Exception as e:
            print("[MQTT] disconnected: {}".format(e))
            self._do_reconnect()

    def _do_reconnect(self):
        """Reconnect and restore subscriptions + call app-level callback."""
        print("[MQTT] Reconnecting...")
        try:
            self._client.reconnect()
            self._last_ping = time.ticks_ms()
            print("[MQTT] Reconnected")

            # Re-announce online (Last Will may have fired)
            self.publish(_STATUS_TOPIC, "online", retain=True)

            # Re-subscribe (broker may have dropped session)
            for topic in self._subs:
                self._client.subscribe(topic)

            # Notify app to re-publish manifest + ISM states
            if self._on_reconnect:
                self._on_reconnect()

        except Exception as e:
            print("[MQTT] Reconnect failed: {}".format(e))
