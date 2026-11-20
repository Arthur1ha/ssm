from types import SimpleNamespace

import pytest

from scripts import trace_mqtt


@pytest.mark.parametrize("dedicated_account", [True, False])
def test_trace_loads_dotenv_as_data_and_resubscribes(tmp_path, monkeypatch, capsys,
                                                   dedicated_account):
    marker = tmp_path / "must-not-exist"
    password = f"opaque $(touch {marker}) `touch {marker}`"
    config = tmp_path / ".env"
    contents = ("MQTT_BROKER_HOST=test-broker.invalid\nMQTT_BROKER_PORT=2883\n"
                "MQTT_USER=legacy-test\nMQTT_PASSWORD='legacy-password'\n")
    if dedicated_account:
        contents += f"MQTT_API_USER=api-test\nMQTT_API_PASSWORD='{password}'\n"
    else:
        contents += f"MQTT_PASSWORD='{password}'\n"
    config.write_text(contents)
    monkeypatch.setattr(trace_mqtt, "ENV_FILE", config)
    for key in ("MQTT_API_USER", "MQTT_API_PASSWORD", "MQTT_USER", "MQTT_PASSWORD",
                "MQTT_BROKER_HOST", "MQTT_BROKER_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MQTT_BROKER_HOST", "stale-inherited-host")
    monkeypatch.setenv("MQTT_BROKER_PORT", "1883")
    # Let monkeypatch restore values added by load_dotenv as well.
    for key in ("MQTT_API_USER", "MQTT_API_PASSWORD", "MQTT_USER", "MQTT_PASSWORD"):
        monkeypatch.setenv(key, "")

    class Client:
        def __init__(self):
            self.subscriptions = []
            self.disconnected = False

        def username_pw_set(self, username, password):
            self.auth = (username, password)

        def reconnect_delay_set(self, **kwargs):
            pass

        def connect_async(self, host, port, **kwargs):
            self.address = (host, port)

        def subscribe(self, topic):
            self.subscriptions.append(topic)

        def loop_forever(self, *, retry_first_connection):
            assert retry_first_connection
            self.on_connect(self, None, {}, 0)
            self.on_message(self, None, SimpleNamespace(topic="ssm/test/state",
                                                        payload=b"hello\xff"))
            self.on_disconnect(self, None, 1)
            self.on_connect(self, None, {}, 0)
            raise KeyboardInterrupt

        def disconnect(self):
            self.disconnected = True

    client = Client()
    monkeypatch.setattr(trace_mqtt.mqtt, "Client", lambda: client)
    assert trace_mqtt.main() == 0
    assert client.address == ("test-broker.invalid", 2883)
    assert client.auth == ("api-test" if dedicated_account else "legacy-test", password)
    assert client.subscriptions == ["ssm/#", "ssm/#"]
    assert client.disconnected
    assert not marker.exists()
    output = capsys.readouterr()
    assert "ssm/test/state hello\ufffd" in output.out
    assert "自动重连" in output.err
    assert password not in output.out + output.err
