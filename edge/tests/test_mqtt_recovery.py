"""Simulate a dropped broker and WiFi without running MicroPython hardware."""
import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest.mock import patch


EDGE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EDGE))


class Clock:
    now = 0

    def ticks_ms(self):
        return self.now

    def ticks_diff(self, later, earlier):
        return later - earlier


class Wlan:
    connected = True
    attempts = 0

    def isconnected(self):
        return self.connected

    def active(self, value):
        pass

    def connect(self, ssid, password):
        self.attempts += 1


class Socket:
    def close(self):
        pass

    def settimeout(self, seconds):
        pass


class BrokerClient:
    attempts = 0
    fail_connect = False
    fail_publish = False
    fail_check = False
    instances = []

    def __init__(self, **kwargs):
        self.sock = Socket()
        self.subscriptions = []
        self.messages = []
        self.instances.append(self)

    def set_last_will(self, *args, **kwargs):
        pass

    def set_callback(self, callback):
        self.callback = callback

    def connect(self, timeout=None):
        type(self).attempts += 1
        if self.fail_connect:
            raise OSError('broker offline')

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)

    def publish(self, topic, data, retain=False, qos=0):
        if self.fail_publish:
            raise OSError('socket lost')
        self.messages.append((topic, data, retain))

    def ping(self):
        pass

    def check_msg(self):
        if self.fail_check:
            raise OSError('socket lost')


class MqttRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.wlan = Wlan()
        BrokerClient.attempts = 0
        BrokerClient.fail_connect = False
        BrokerClient.fail_publish = False
        BrokerClient.fail_check = False
        BrokerClient.instances = []
        network = types.ModuleType('network')
        network.STA_IF = 1
        network.WLAN = lambda _: self.wlan
        umqtt = types.ModuleType('umqtt')
        simple = types.ModuleType('umqtt.simple')
        simple.MQTTClient = BrokerClient
        with patch.dict(sys.modules, {'network': network, 'umqtt': umqtt,
                                      'umqtt.simple': simple, 'ujson': json}):
            spec = importlib.util.spec_from_file_location('edge_mqtt_under_test',
                                                          EDGE / 'mqtt_client.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        module.time = self.clock
        self.module = module

    def test_broker_outage_does_not_spin_and_republishes_on_reconnect(self):
        client = self.module.MqttClient()
        seen = []
        client.set_callback(lambda topic, data: seen.append((topic, data)))
        client.subscribe('ssm/task/led/+')
        client.set_reconnect_callback(lambda: seen.append('snapshot'))
        BrokerClient.fail_connect = True
        client.begin()
        self.assertEqual(BrokerClient.attempts, 1)
        self.assertFalse(client.publish('state', {'ism': 'ON'}))
        for _ in range(100):
            self.clock.now += 10
            client.check_msg()
        self.assertEqual(BrokerClient.attempts, 1)
        BrokerClient.fail_connect = False
        self.clock.now = 5000
        client.check_msg()
        self.assertEqual(BrokerClient.attempts, 2)
        self.assertEqual(seen, ['snapshot'])
        self.assertEqual(BrokerClient.instances[-1].subscriptions,
                         ['ssm/task/led/+'])

        BrokerClient.fail_publish = True
        self.assertFalse(client.publish('state', {'ism': 'OFF'}))
        self.assertIsNone(client._client)
        BrokerClient.fail_publish = False
        self.clock.now = 10000
        client.check_msg()
        self.assertEqual(seen, ['snapshot', 'snapshot'])

    def test_wifi_outage_retries_without_attempting_mqtt(self):
        self.wlan.connected = False
        client = self.module.MqttClient()
        client.begin()
        self.assertEqual(self.wlan.attempts, 1)
        self.assertEqual(BrokerClient.attempts, 0)
        for _ in range(100):
            self.clock.now += 10
            client.check_msg()
        self.assertEqual(self.wlan.attempts, 1)
        self.wlan.connected = True
        self.clock.now = 5000
        client.check_msg()
        self.assertEqual(BrokerClient.attempts, 1)


if __name__ == '__main__':
    unittest.main()
