"""CPython simulation of the MicroPython LED driver and MQTT bridge."""
import importlib
import pathlib
import sys
import types
import unittest

EDGE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EDGE))
sys.modules.setdefault("neopixel", types.ModuleType("neopixel"))
machine = types.ModuleType("machine")
machine.ADC = type("ADC", (), {})
machine.Pin = type("Pin", (), {})
sys.modules.setdefault("machine", machine)
probe = types.ModuleType("probe")
probe.PRESENCE = {}
sys.modules.setdefault("probe", probe)

from config import UNIT_LED
from ism import ISM, LED_TABLE, State, Trigger
from trigger_map import TriggerMap
bsm_module = importlib.import_module("bsm")


class Clock:
    now = 0

    def ticks_ms(self):
        return self.now

    def ticks_diff(self, later, earlier):
        return later - earlier


class Pixels:
    def __init__(self):
        self.values = [(0, 0, 0)] * 2

    def __setitem__(self, index, value):
        self.values[index] = value

    def write(self):
        pass


class Mqtt:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, retain=False):
        self.messages.append((topic, payload.copy(), retain))

    def states(self):
        return [p for t, p, retained in self.messages
                if t == 'ssm/agents/{}/state'.format(UNIT_LED) and retained]

    def results(self):
        return [p for t, p, _ in self.messages if t.startswith('ssm/result/')]


class Local:
    pass


class LedTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        bsm_module.time = self.clock
        self.pixels = Pixels()
        self.bsm = bsm_module.BSM.__new__(bsm_module.BSM)
        self.bsm._np = self.pixels
        self.bsm._num_pixels = 2
        self.bsm._blink_id = 0
        self.bsm._blinking = False
        self.bsm._led_r = self.bsm._led_g = self.bsm._led_b = 0
        self.bsm._led_brightness = 0
        self.mqtt = Mqtt()
        self.ism = ISM(State.OFF, UNIT_LED, LED_TABLE)
        self.bridge = TriggerMap(self.bsm, self.ism, None, None, self.mqtt, Local())
        self.bsm._event_cb = self.bridge.on_bsm_event

    def command(self, task_id, action, **params):
        self.bridge._handle_led_task({'action': action, 'params': params,
                                      'session_id': 's'}, task_id)

    def finish_blink(self, count=1):
        for _ in range(count * 2):
            self.clock.now += 300
            self.bsm._tick_blink(self.clock.now)

    def test_transition_table_and_dynamic_completion(self):
        self.assertTrue(self.ism.transition(Trigger.CMD_OFF))
        self.assertTrue(self.ism.transition(Trigger.CMD_ON))
        self.assertTrue(self.ism.transition(Trigger.CMD_ON))
        self.assertTrue(self.ism.transition(Trigger.CMD_COLOR))
        self.assertTrue(self.ism.transition(Trigger.CMD_BLINK))
        self.assertTrue(self.ism.transition(Trigger.CMD_BLINK))
        self.assertFalse(self.ism.transition(Trigger.BLINK_DONE))
        self.assertTrue(self.ism.transition(Trigger.BLINK_DONE, State.ON))
        self.assertEqual(self.ism.state, State.ON)

    def test_color_off_memory_and_retained_snapshot(self):
        self.bridge._publish_led_state(force=True)
        self.command('on', 'SET_STATE', state='ON')
        self.assertEqual(self.ism.state, State.ON)
        self.assertEqual(self.mqtt.states()[-1]['brightness'], 200)
        self.command('red', 'SET_COLOR', r=255, g=0, b=0, brightness=180)
        self.assertEqual(self.pixels.values[0], (100, 0, 0))
        self.command('zero', 'SET_COLOR', r=0, g=0, b=0, brightness=180)
        self.assertEqual(self.ism.state, State.OFF)
        self.assertEqual(self.pixels.values[0], (0, 0, 0))
        self.assertEqual(self.mqtt.states()[-1]['r'], 0)
        self.command('again', 'SET_STATE', state='ON')
        self.assertEqual(self.mqtt.states()[-1]['r'], 255)
        self.assertEqual(self.mqtt.states()[-1]['brightness'], 180)
        self.command('dark', 'SET_COLOR', r=5, g=5, b=5, brightness=0)
        self.assertEqual(self.ism.state, State.OFF)
        self.command('repeat_off', 'SET_STATE', state='OFF')
        self.assertEqual(self.mqtt.results()[-1]['result'], 'ok')
        self.assertEqual(len(self.mqtt.states()), 6)
        self.assertTrue(all('agent_id' in s and 'ts' in s for s in self.mqtt.states()))

    def test_blink_restore_and_restart_cancels_old_task(self):
        self.command('color', 'SET_COLOR', r=0, g=255, b=0, brightness=100)
        self.command('first', 'BLINK', r=255, g=0, b=0, count=2)
        old_id = self.bridge._blink_id
        self.assertEqual(self.mqtt.states()[-1]['count'], 2)
        self.assertEqual(self.mqtt.results()[-1]['result'], 'executing')
        self.command('second', 'BLINK', r=0, g=0, b=255, count=1)
        self.assertNotEqual(old_id, self.bridge._blink_id)
        self.assertEqual(self.mqtt.results()[-2]['result'], 'cancelled')
        self.bridge.on_bsm_event('BLINK_DONE', {'blink_id': old_id})
        self.assertEqual(self.ism.state, State.BLINKING)
        self.finish_blink()
        self.assertEqual(self.ism.state, State.ON)
        self.assertEqual(self.pixels.values[0], (0, 100, 0))
        self.assertEqual(self.mqtt.results()[-1]['result'], 'ok')
        self.assertEqual(self.mqtt.states()[-1]['brightness'], 100)

    def test_off_blink_and_command_cancellation(self):
        self.command('blink', 'BLINK', count=1)
        self.finish_blink()
        self.assertEqual(self.ism.state, State.OFF)
        self.assertEqual(self.pixels.values[0], (0, 0, 0))
        self.command('blink2', 'BLINK', count=2)
        old_id = self.bridge._blink_id
        self.command('off', 'SET_STATE', state='OFF')
        self.assertEqual(self.mqtt.results()[-2]['result'], 'cancelled')
        self.bridge.on_bsm_event('BLINK_DONE', {'blink_id': old_id})
        self.assertEqual(self.ism.state, State.OFF)
        self.assertEqual(self.mqtt.results()[-1]['result'], 'ok')

    def test_color_interrupts_blink_without_late_completion(self):
        self.command('blink', 'BLINK', count=2)
        old_id = self.bridge._blink_id
        self.command('color', 'SET_COLOR', r=0, g=0, b=255, brightness=255)
        self.assertEqual(self.ism.state, State.ON)
        self.assertEqual(self.mqtt.results()[-2]['result'], 'cancelled')
        result_count = len(self.mqtt.results())
        self.clock.now += 1000
        self.bsm._tick_blink(self.clock.now)
        self.bridge.on_bsm_event('BLINK_DONE', {'blink_id': old_id})
        self.assertEqual(len(self.mqtt.results()), result_count)
        self.assertEqual(self.pixels.values[0], (0, 0, 100))

    def test_compat_alias_and_reconnect_snapshot(self):
        self.command('dim', 'SET_STATE', state='DIM')
        self.assertEqual(self.ism.state, State.ON)
        self.assertEqual(self.mqtt.states()[-1]['brightness'], 40)
        count = len(self.mqtt.states())
        self.command('dim2', 'SET_STATE', state='DIM')
        self.assertEqual(len(self.mqtt.states()), count)
        self.bridge._publish_led_state(force=True)
        self.assertEqual(len(self.mqtt.states()), count + 1)


if __name__ == '__main__':
    unittest.main()
