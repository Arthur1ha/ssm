"""
腾讯位置服务 — 智能硬件WiFi定位
POST https://apis.map.qq.com/ws/location/v1/network

参数：
  key        - 腾讯位置服务 API Key
  device_id  - 设备唯一标识（用ESP32 MAC地址即可）
  wifi_list  - [{mac, rssi}, ...]  至少1条，建议3条以上

响应：
  status=0   → 成功，result.location.lat/lng
  status=121 → 今日配额已满
  status=348 → 参数缺失

生产流程：ESP32 scan() → MQTT → PC Agent → 此API → 发布定位结果
"""

import requests
import json

KEY = "CNNBZ-6XU6Z-YZOXU-TSKSA-WT5ZH-I2BW7"

# ── 模拟 ESP32 WiFi 扫描结果 ──────────────────────────────────────────
# 实际来自 MicroPython:
#   sta = network.WLAN(network.STA_IF)
#   for ssid, bssid, ch, rssi, auth, hidden in sta.scan():
#       mac = ':'.join(f'{b:02x}' for b in bssid)
MOCK_WIFI_SCAN = [
    {"mac": "70:ba:ef:d0:87:91", "rssi": -42},
    {"mac": "70:ba:ef:d1:0e:01", "rssi": -55},
    {"mac": "c8:3a:35:00:11:22", "rssi": -68},
    {"mac": "f0:1a:2b:3c:4d:5e", "rssi": -72},
]

DEVICE_ID = "esp32_desk_001"  # 生产中用 ubinascii.hexlify(network.WLAN().config('mac'))


def locate(wifi_list: list) -> dict:
    payload = {
        "key": KEY,
        "device_id": DEVICE_ID,
        "wifi_list": [{"mac": ap["mac"], "rssi": ap["rssi"]} for ap in wifi_list],
    }

    print(f"→ POST https://apis.map.qq.com/ws/location/v1/network")
    print(f"  wifi_list: {len(wifi_list)} APs")

    r = requests.post(
        "https://apis.map.qq.com/ws/location/v1/network",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    print(f"← HTTP {r.status_code}: {r.text}\n")
    return r.json()


def main():
    print("=" * 60)
    print("腾讯位置服务 WiFi定位 — 接口测试")
    print("=" * 60 + "\n")

    result = locate(MOCK_WIFI_SCAN)

    status = result.get("status", -1)
    if status == 0:
        loc = result["result"]["location"]
        print(f"定位成功!")
        print(f"  纬度: {loc['lat']}")
        print(f"  经度: {loc['lng']}")
        print(f"  精度: {result['result'].get('accuracy', '?')} 米")
    elif status == 121:
        print("今日 API 配额已满，明天再试（格式已验证正确）")
    else:
        print(f"失败: status={status}, message={result.get('message')}")


if __name__ == "__main__":
    main()
