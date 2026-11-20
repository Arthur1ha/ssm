#!/usr/bin/env python3
"""Print MQTT traffic without executing dotenv contents or publishing messages."""

import os
import sys
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parents[1] / "cloud" / ".env"


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe("ssm/#")
        print("MQTT 已连接，订阅 ssm/#", flush=True)
    else:
        print(f"MQTT 连接失败：{mqtt.connack_string(rc)}", file=sys.stderr, flush=True)


def on_disconnect(client, userdata, rc):
    if rc != 0:
        print("MQTT 连接断开，等待自动重连...", file=sys.stderr, flush=True)


def on_message(client, userdata, message):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message.topic,
          message.payload.decode("utf-8", errors="replace"), flush=True)


def main() -> int:
    load_dotenv(ENV_FILE, override=True)
    client = mqtt.Client()
    username = os.getenv("MQTT_API_USER") or os.getenv("MQTT_USER", "")
    password = os.getenv("MQTT_API_PASSWORD") or os.getenv("MQTT_PASSWORD", "")
    if username:
        client.username_pw_set(username, password)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=30)
    try:
        client.connect_async(os.getenv("MQTT_BROKER_HOST", "127.0.0.1"),
                             int(os.getenv("MQTT_BROKER_PORT", "1883")), keepalive=60)
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
