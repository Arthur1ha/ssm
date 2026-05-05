# boot.py — runs first on every ESP32 power-on / reset
# Connects to WiFi. If connection fails, halts with error LED blink.
# main.py runs only after this succeeds.

import network
import time
import neopixel
from machine import Pin
from config import WIFI_SSID, WIFI_PASSWORD, WS2812_PIN, WS2812_NUM

def _blink_error():
    """WiFi 连接失败时用 WS2812 红色闪烁提示。"""
    try:
        np = neopixel.NeoPixel(Pin(WS2812_PIN), WS2812_NUM)
        for _ in range(20):
            for i in range(WS2812_NUM):
                np[i] = (80, 0, 0)
            np.write()
            time.sleep_ms(150)
            for i in range(WS2812_NUM):
                np[i] = (0, 0, 0)
            np.write()
            time.sleep_ms(150)
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
