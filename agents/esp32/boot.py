# boot.py — runs first on every ESP32 power-on / reset
# Connects to WiFi. If connection fails, halts with error LED blink.
# main.py runs only after this succeeds.

import network
import time
from machine import Pin, PWM
from config import WIFI_SSID, WIFI_PASSWORD, PIN_R, PIN_G, PIN_B, PWM_FREQ

def _blink_error():
    """Rapid red blink on the RGB LED to signal WiFi failure."""
    try:
        r = PWM(Pin(PIN_R), freq=PWM_FREQ, duty=512)
        for _ in range(20):
            r.duty(512)
            time.sleep_ms(150)
            r.duty(0)
            time.sleep_ms(150)
        r.deinit()
    except Exception:
        pass

wlan = network.WLAN(network.STA_IF)
wlan.active(True)

if not wlan.isconnected():
    print(f"[boot] Connecting to WiFi: {WIFI_SSID}")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    deadline = time.ticks_add(time.ticks_ms(), 15000)  # 15s timeout
    while not wlan.isconnected():
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            print("[boot] WiFi timeout — halting")
            _blink_error()
            import sys
            sys.exit()
        time.sleep_ms(200)

ip = wlan.ifconfig()[0]
print(f"[boot] WiFi OK — IP: {ip}")
