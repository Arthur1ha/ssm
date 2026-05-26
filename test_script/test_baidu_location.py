"""
测试脚本：百度智能硬件定位API
API: https://api.map.baidu.com/locapi/v2  (POST)
文档: https://lbsyun.baidu.com/faq/api?title=webapi/intel-hardware-base

macs 格式: "mac,rssi,ssid|mac,rssi,ssid|..."  (管道分隔, ssid可省略)
"""

import requests
import json
import time

AK = "e46DPIA7V0XdED6zBDAUnr4PHRUV2CN6"

# ── 模拟 ESP32 扫描到的 WiFi 列表 ────────────────────────────────────
# 实际来自 MicroPython: network.WLAN(network.STA_IF).scan()
# scan() 返回: (ssid, bssid_bytes, channel, rssi, authmode, hidden)
MOCK_WIFI_SCAN = [
    {"mac": "70:ba:ef:d0:87:91", "rssi": -42, "ssid": "ChinaNet-Home"},
    {"mac": "70:ba:ef:d1:0e:01", "rssi": -55, "ssid": "TP-Link_XXXX"},
    {"mac": "c8:3a:35:00:11:22", "rssi": -68, "ssid": "MERCURY_XXXX"},
]

DEVICE_ID = "esp32_desk_001"  # 唯一设备ID，生产用ESP32 MAC


def build_macs_string(wifi_list: list) -> str:
    """
    转换成百度API要求的 macs 格式:
    "mac,rssi,ssid|mac,rssi,ssid|..."
    """
    parts = [f"{ap['mac']},{ap['rssi']},{ap.get('ssid', '')}" for ap in wifi_list]
    return "|".join(parts)


def format_mac_from_bytes(bssid_bytes: bytes) -> str:
    """MicroPython scan() 返回 bytes bssid → xx:xx:xx:xx:xx:xx"""
    return ":".join(f"{b:02x}" for b in bssid_bytes)


def call_baidu_hardware_location(wifi_list: list) -> dict:
    url = "https://api.map.baidu.com/locapi/v2"

    macs_str = build_macs_string(wifi_list)

    payload = {
        # ── 认证字段 ──
        "key":   AK,
        "src":   "ssm.esp32",     # 自定义来源标识
        "prod":  "loc",           # 产品线
        "ver":   "2.1",
        "trace": True,

        # ── 定位类型 ──
        "accesstype": 1,          # 1 = WiFi接入
        "imei": DEVICE_ID,        # 设备唯一标识

        # ── 时间戳 ──
        "ctime": str(int(time.time())),

        # ── WiFi 数据 ──
        "macs": macs_str,         # "mac,rssi,ssid|mac,rssi,ssid|..."
    }

    headers = {"Content-Type": "application/json"}

    print(f"→ POST {url}")
    print(f"  macs: {macs_str}")
    print(f"  payload: {json.dumps(payload, ensure_ascii=False, indent=2)}")

    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    print(f"\n← HTTP {resp.status_code}")
    print(f"  body: {resp.text}")

    return resp.json() if resp.ok else {"error": -1, "raw": resp.text}


def main():
    print("=" * 60)
    print("百度智能硬件定位 — 接口测试")
    print("=" * 60)

    result = call_baidu_hardware_location(MOCK_WIFI_SCAN)

    print("\n── 解析结果 ──")
    err = result.get("error", result.get("errcode", -1))

    if err == 0:
        loc = result.get("location", "")  # "longitude,latitude"
        lng, lat = loc.split(",") if loc else ("?", "?")
        radius = result.get("radius", "?")
        loc_type = {0: "无", 1: "GPS", 2: "WiFi", 3: "混合", 4: "基站", 5: "其他"}.get(
            result.get("type", -1), "未知"
        )
        print(f"定位成功!")
        print(f"  经度: {lng}")
        print(f"  纬度: {lat}")
        print(f"  精度半径: {radius} 米")
        print(f"  定位方式: {loc_type}")
        print(f"  城市: {result.get('city', '?')} {result.get('district', '')}")
    else:
        msg = result.get("msg", result.get("message", result.get("raw", "unknown")))
        print(f"定位失败: error={err}, msg={msg}")


if __name__ == "__main__":
    main()
