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

# 第一时间拉低数据线，防止浮空期间 WS2812 锁存噪声
try:
    _np = neopixel.NeoPixel(Pin(WS2812_PIN), WS2812_NUM)
    for _i in range(WS2812_NUM):
        _np[_i] = (0, 0, 0)
    _np.write()
    del _np, _i
except Exception:
    pass

import machine

wlan = network.WLAN(network.STA_IF)
wlan.active(True)

MAX_RETRIES = 5       # 最多重试 5 次
TIMEOUT_MS  = 20000  # 每次等待 20 秒

if not wlan.isconnected():
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"[boot] Connecting to WiFi: {WIFI_SSID} (attempt {attempt}/{MAX_RETRIES})")
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)

        deadline = time.ticks_add(time.ticks_ms(), TIMEOUT_MS)
        while not wlan.isconnected():
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                print(f"[boot] WiFi timeout (attempt {attempt})")
                wlan.disconnect()
                time.sleep_ms(2000)  # 断开后等 2s 再重试
                break
            time.sleep_ms(200)

        if wlan.isconnected():
            break
    else:
        # 全部重试失败：闪红灯后自动重启，不永久停机
        print("[boot] WiFi failed after all retries — rebooting in 5s")
        _blink_error()
        time.sleep_ms(5000)
        machine.reset()

ip = wlan.ifconfig()[0]
print(f"[boot] WiFi OK — IP: {ip}")
