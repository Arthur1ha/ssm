# probe.py — Generic hardware presence detection engine.
# Reads probe specs from UNIT_CONFIGS; zero unit-specific logic here.
# Runs at import time (after boot.py WiFi init), before BSM init.
import time
from machine import Pin, ADC
from config import UNIT_CONFIGS

_PULL = {'up': Pin.PULL_UP, 'down': Pin.PULL_DOWN}


def _probe_digital(spec):
    """
    Pull resistor trick: a connected sensor's output overrides the weak internal pull.

    Params (all in spec dict):
      pin         – GPIO number
      pull        – 'up' | 'down'
      active      – level the connected sensor drives when idle (0 or 1)
      n           – sample count (default 20)
      min_hits    – minimum samples matching active level to count as present
                    default: n // 2  (50% majority)
                    set lower (e.g. 3) for sensors that occasionally leave idle state
    """
    n        = spec.get('n', 20)
    min_hits = spec.get('min_hits', n // 2)
    active   = spec['active']

    p = Pin(spec['pin'], Pin.IN, _PULL[spec['pull']])
    time.sleep_ms(30)   # let pull resistor + sensor output settle

    hits = 0
    for _ in range(n):
        if p.value() == active:
            hits += 1
        time.sleep_ms(5)   # spread samples over ~100ms window

    Pin(spec['pin'], Pin.IN)   # release — no pull
    return hits >= min_hits


def _probe_adc(spec):
    """Floating ADC pin reads near 0; a connected sensor reads above min_val."""
    adc = ADC(Pin(spec['pin']))
    adc.atten(ADC.ATTN_11DB)
    val = sum(adc.read() for _ in range(8)) // 8
    return val > spec.get('min_val', 30)


def _probe_one(spec):
    t = spec['type']
    if t == 'flag':    return bool(spec['enabled'])
    if t == 'digital': return _probe_digital(spec)
    if t == 'adc':     return _probe_adc(spec)
    return True   # unknown type → assume present


# Run detection for every unit declared in UNIT_CONFIGS.
PRESENCE = {uid: _probe_one(cfg['probe']) for uid, cfg in UNIT_CONFIGS.items()}

print("[Probe]", {k: v for k, v in PRESENCE.items()})
