# bsm.py — Behavior State Machine
# Internal hardware logic: sensor reading, LED/buzzer driving.
# No MQTT knowledge — communicates upward via event_cb(event_name, data).

import time
import neopixel
from machine import ADC, Pin, PWM
from config import (
    LIGHT_SENSOR_PIN, IR_SENSOR_PIN, SOUND_SENSOR_PIN,
    WS2812_PIN, WS2812_NUM, WS2812_MAX_VAL, BUZZER_PIN, PWM_FREQ,
    LIGHT_DIGITAL_MODE,
    LIGHT_BRIGHT_THRESH, LIGHT_NORMAL_THRESH, LIGHT_DIM_THRESH,
    LIGHT_HYSTERESIS, LIGHT_CONFIRM_COUNT,
    LIGHT_SAMPLE_MS, IR_DEBOUNCE_MS, IR_HEARTBEAT_MS, SOUND_COOLDOWN_MS,
)

# Light levels
LEVEL_DARK   = "DARK"
LEVEL_NORMAL = "NORMAL"
LEVEL_BRIGHT = "BRIGHT"
# DIM only used in ADC mode
LEVEL_DIM    = "DIM"


class BSM:
    def __init__(self, event_cb):
        """
        event_cb(event_name, data_dict) — called when BSM wants to
        communicate upward (sensor event, blink done, etc.)
        """
        self._event_cb = event_cb

        # ── Light sensor ─────────────────────────────────────
        # LIGHT_DIGITAL_MODE=False → ADC on GPIO32-39 (e.g. GPIO34)
        # LIGHT_DIGITAL_MODE=True  → digital DO pin (binary only)
        if LIGHT_DIGITAL_MODE:
            self._light_pin  = Pin(LIGHT_SENSOR_PIN, Pin.IN)
            self._light_adc  = None
        else:
            self._light_adc  = ADC(Pin(LIGHT_SENSOR_PIN))
            self._light_adc.atten(ADC.ATTN_11DB)
            self._light_pin  = None

        self._light_level   = LEVEL_NORMAL
        self._light_raw     = 0
        self._confirm_buf   = []
        self._last_light    = 0

        # ── IR sensor ────────────────────────────────────────
        # D19: active LOW module (0 = object detected)
        self._ir_pin        = Pin(IR_SENSOR_PIN, Pin.IN, Pin.PULL_UP)
        self._ir_presence   = False
        self._ir_raw_prev   = -1
        self._ir_last_change= 0
        self._ir_last_hb    = 0

        # ── Sound sensor ─────────────────────────────────────
        # Digital DO pin: HIGH = sound detected, LOW = quiet
        # One-shot event: only fires on rising edge (quiet→sound)
        self._sound_pin      = Pin(SOUND_SENSOR_PIN, Pin.IN)
        self._sound_prev     = 0
        self._sound_cooldown = 0   # ticks_ms of last fired event

        # ── WS2812 灯环 ──────────────────────────────────────
        self._np          = neopixel.NeoPixel(Pin(WS2812_PIN), WS2812_NUM)
        self._num_pixels  = WS2812_NUM
        self._set_raw(0, 0, 0)   # 启动时全灭
        self._led_r = 0
        self._led_g = 0
        self._led_b = 0
        self._led_brightness = 0

        # Blink state
        self._blink_on    = False
        self._blink_count = 0
        self._blink_last  = 0
        self._blinking    = False
        self._blink_r     = 255
        self._blink_g     = 255
        self._blink_b     = 255

        # ── Buzzer ───────────────────────────────────────────
        self._pwm_buz     = PWM(Pin(BUZZER_PIN), freq=440, duty=0)
        self._buz_pattern = None
        self._buz_step    = 0
        self._buz_last    = 0

    # ── Main loop (call every iteration) ─────────────────────
    def tick(self):
        now = time.ticks_ms()
        self._tick_light(now)
        self._tick_ir(now)
        self._tick_sound(now)
        self._tick_blink(now)
        self._tick_buzzer(now)

    # ─────────────────────────────────────────────────────────
    #  LIGHT SENSOR
    # ─────────────────────────────────────────────────────────
    def _tick_light(self, now):
        if time.ticks_diff(now, self._last_light) < LIGHT_SAMPLE_MS:
            return
        self._last_light = now

        if LIGHT_DIGITAL_MODE:
            self._tick_light_digital()
        else:
            self._tick_light_adc()

    def _tick_light_digital(self):
        """
        D18 has no ADC — binary reading only.
        Module DO pin: LOW = bright (LDR low resistance pulls down),
                       HIGH = dark  (LDR high resistance)
        Adjust inversion below if your module wiring differs.
        """
        raw = self._light_pin.value()
        # Typical LDR module: DO=0 means bright, DO=1 means dark
        new_level = LEVEL_DARK if raw == 1 else LEVEL_BRIGHT
        self._light_raw = raw

        if new_level != self._light_level:
            self._light_level = new_level
            self._event_cb("LIGHT_CHANGED", {
                "value": raw, "level": new_level
            })
        else:
            self._event_cb("LIGHT_HEARTBEAT", {
                "value": raw, "level": self._light_level
            })

    def _tick_light_adc(self):
        """ADC mode — gradient levels. Only usable on GPIO32-39."""
        raw = self._light_adc.read()
        self._light_raw = raw
        new_level = self._classify_light(raw)

        self._confirm_buf.append(new_level)
        if len(self._confirm_buf) > LIGHT_CONFIRM_COUNT:
            self._confirm_buf.pop(0)

        if (len(self._confirm_buf) == LIGHT_CONFIRM_COUNT
                and all(l == new_level for l in self._confirm_buf)
                and new_level != self._light_level):
            self._light_level = new_level
            self._event_cb("LIGHT_CHANGED", {"value": raw, "level": new_level})
        else:
            self._event_cb("LIGHT_HEARTBEAT", {"value": raw, "level": self._light_level})

    def _classify_light(self, raw):
        lvl = self._light_level
        # → BRIGHT
        if lvl != LEVEL_BRIGHT and raw > LIGHT_BRIGHT_THRESH + LIGHT_HYSTERESIS:
            return LEVEL_BRIGHT
        # BRIGHT → NORMAL
        if lvl == LEVEL_BRIGHT and raw < LIGHT_BRIGHT_THRESH - LIGHT_HYSTERESIS:
            return LEVEL_NORMAL
        # NORMAL/BRIGHT → DIM  (NORMAL_THRESH 是 NORMAL/DIM 分界)
        if lvl not in (LEVEL_DIM, LEVEL_DARK) and raw < LIGHT_NORMAL_THRESH - LIGHT_HYSTERESIS:
            return LEVEL_DIM
        # DIM/DARK → NORMAL
        if lvl in (LEVEL_DIM, LEVEL_DARK) and raw > LIGHT_NORMAL_THRESH + LIGHT_HYSTERESIS:
            return LEVEL_NORMAL
        # DIM → DARK  (DIM_THRESH 是 DIM/DARK 分界)
        if lvl == LEVEL_DIM and raw < LIGHT_DIM_THRESH - LIGHT_HYSTERESIS:
            return LEVEL_DARK
        # DARK → DIM
        if lvl == LEVEL_DARK and raw > LIGHT_DIM_THRESH + LIGHT_HYSTERESIS:
            return LEVEL_DIM
        return lvl

    @property
    def light_level(self): return self._light_level
    @property
    def light_raw(self):   return self._light_raw

    # ─────────────────────────────────────────────────────────
    #  IR SENSOR  (D19, active LOW)
    # ─────────────────────────────────────────────────────────
    def _tick_ir(self, now):
        raw = self._ir_pin.value()

        if raw != self._ir_raw_prev:
            self._ir_raw_prev     = raw
            self._ir_last_change  = now

        if time.ticks_diff(now, self._ir_last_change) >= IR_DEBOUNCE_MS:
            presence = (raw == 0)   # active LOW: 0 = detected
            if presence != self._ir_presence:
                self._ir_presence = presence
                self._event_cb("IR_CHANGED", {"presence": presence, "raw": raw})
                self._ir_last_hb  = now

        if time.ticks_diff(now, self._ir_last_hb) >= IR_HEARTBEAT_MS:
            self._ir_last_hb = now
            self._event_cb("IR_HEARTBEAT", {"presence": self._ir_presence, "raw": raw})

    @property
    def ir_presence(self): return self._ir_presence

    # ─────────────────────────────────────────────────────────
    #  SOUND SENSOR  (D15, rising edge = sound detected)
    # ─────────────────────────────────────────────────────────
    def _tick_sound(self, now):
        raw = self._sound_pin.value()

        # Rising edge detection + cooldown
        if raw == 1 and self._sound_prev == 0:
            if time.ticks_diff(now, self._sound_cooldown) >= SOUND_COOLDOWN_MS:
                self._sound_cooldown = now
                self._event_cb("SOUND_DETECTED", {"ts": time.time()})

        self._sound_prev = raw

    # ─────────────────────────────────────────────────────────
    #  RGB LED
    # ─────────────────────────────────────────────────────────
    def led_set_color(self, r, g, b, brightness=255):
        self._led_r = r
        self._led_g = g
        self._led_b = b
        self._led_brightness = brightness
        self._blinking = False
        self._apply_led()

    def led_set_state(self, state):
        self._blinking = False
        if state == "OFF":
            self._led_r = self._led_g = self._led_b = 0
            self._led_brightness = 0
        elif state == "BRIGHT":
            self._led_r = self._led_g = self._led_b = 255
            self._led_brightness = 255
        elif state == "DIM":
            self._led_r = self._led_g = self._led_b = 255
            self._led_brightness = 40
        self._apply_led()

    def led_blink(self, r=255, g=255, b=255, count=3):
        self._blink_r     = r
        self._blink_g     = g
        self._blink_b     = b
        self._blink_count = count * 2
        self._blink_last  = 0
        self._blink_on    = False
        self._blinking    = True

    def _tick_blink(self, now):
        if not self._blinking:
            return
        if time.ticks_diff(now, self._blink_last) < 300:
            return
        self._blink_last = now
        self._blink_on = not self._blink_on
        if self._blink_on:
            self._set_raw(self._blink_r, self._blink_g, self._blink_b)
        else:
            self._set_raw(0, 0, 0)
            self._blink_count -= 1
            if self._blink_count <= 0:
                self._blinking = False
                self._event_cb("BLINK_DONE", {})

    def _apply_led(self):
        bri = self._led_brightness / 255.0
        self._set_raw(
            int(self._led_r   * bri),
            int(self._led_g   * bri),
            int(self._led_b   * bri)
        )

    def _set_raw(self, r, g, b):
        r = min(r, WS2812_MAX_VAL)
        g = min(g, WS2812_MAX_VAL)
        b = min(b, WS2812_MAX_VAL)
        for i in range(self._num_pixels):
            self._np[i] = (r, g, b)
        self._np.write()

    # ─────────────────────────────────────────────────────────
    #  BUZZER
    # ─────────────────────────────────────────────────────────
    _PATTERNS = {
        "NOTIFY": [(880, 80), (0, 40), (1047, 100)],
        "ALERT":  [(880, 200), (0, 100), (880, 200), (0, 100), (880, 200)],
    }

    def buzzer_play(self, pattern_name):
        self._buz_pattern = self._PATTERNS.get(pattern_name, [])
        self._buz_step    = 0
        self._buz_last    = time.ticks_ms()
        self._run_buzzer_step()

    def buzzer_stop(self):
        self._buz_pattern = None
        self._pwm_buz.duty(0)
        self._event_cb("SOUND_DONE", {})

    def _tick_buzzer(self, now):
        if not self._buz_pattern or self._buz_step >= len(self._buz_pattern):
            return
        _, dur = self._buz_pattern[self._buz_step]
        if time.ticks_diff(now, self._buz_last) >= dur:
            self._buz_step += 1
            self._buz_last  = now
            if self._buz_step < len(self._buz_pattern):
                self._run_buzzer_step()
            else:
                self._pwm_buz.duty(0)
                self._buz_pattern = None
                self._event_cb("SOUND_DONE", {})

    def _run_buzzer_step(self):
        freq, _ = self._buz_pattern[self._buz_step]
        if freq == 0:
            self._pwm_buz.duty(0)
        else:
            self._pwm_buz.freq(freq)
            self._pwm_buz.duty(512)
