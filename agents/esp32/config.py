# config.py — All constants for SSM ESP32 agent
# Edit WIFI_SSID, WIFI_PASSWORD, MQTT_BROKER_IP before uploading

# ── WiFi ─────────────────────────────────────────────────────
WIFI_SSID     = "Arthur"
WIFI_PASSWORD = "66666666"
                                                                                                            
# ── Location (fixed coordinates, GCJ-02) ─────────────────────
LOCATION_LNG = 121.43235
LOCATION_LAT  = 31.029672

# ── MQTT ─────────────────────────────────────────────────────
MQTT_BROKER_IP = "47.116.137.202"  # cloud server public IP
MQTT_PORT      = 1883
MQTT_USER      = "ssm_user"        # broker auth username
MQTT_PASSWORD  = "Wl4sErQrlrpEbm7r"
AGENT_ID       = "esp32_desk"
FIRMWARE_VER   = "0.2.0"

# Per-unit agent IDs
AGENT_LIGHT = AGENT_ID + "_light"
AGENT_IR    = AGENT_ID + "_ir"
AGENT_SOUND = AGENT_ID + "_sound"
AGENT_LED   = AGENT_ID + "_led"
AGENT_BUZ   = AGENT_ID + "_buz"

# ── Hardware Pins（实际接线）─────────────────────────────────
BUZZER_PIN       = 5    # D5  — 无源蜂鸣器 PWM 输出
BUZZER_ENABLED   = False
WS2812_PIN       = 4    # D4  — WS2812 灯环数据线
WS2812_NUM       = 16   # 灯环像素数量
WS2812_MAX_VAL   = 100  # 单通道最大值（0-255），限制峰值电流防止过热
SOUND_SENSOR_PIN = 15   # D15 — 声音传感器 数字输入（高=检测到声音，启动后正常）
LIGHT_SENSOR_PIN = 34   # GPIO34 — 光敏传感器 AO 模拟输入（ADC1，不受 WiFi 影响）
IR_SENSOR_PIN    = 19   # D19 — 红外传感器 数字输入（低电平=检测到）

PWM_FREQ         = 5000  # Hz for buzzer base freq

# ── 光敏传感器模式 ────────────────────────────────────────────
# D18 没有 ADC，只能数字读取 → LIGHT_DIGITAL_MODE = True
# 若改接 GPIO34/36，将此设为 False 可启用渐变等级
LIGHT_DIGITAL_MODE   = False

# 以下阈值仅在 LIGHT_DIGITAL_MODE = False（ADC 模式）时生效
LIGHT_BRIGHT_THRESH  = 3000
LIGHT_NORMAL_THRESH  = 1600
LIGHT_DIM_THRESH     = 600
LIGHT_HYSTERESIS     = 50
LIGHT_CONFIRM_COUNT  = 2

# ── Timing (milliseconds) ─────────────────────────────────────
LIGHT_SAMPLE_MS      = 2000   # light polling interval
IR_DEBOUNCE_MS       = 200    # IR presence debounce
IR_HEARTBEAT_MS      = 30000  # IR heartbeat even when no change
SOUND_COOLDOWN_MS    = 2000   # minimum gap between sound events
LOCAL_RULES_DELAY_MS = 500    # delay after MQTT connect before local rules fire
HEARTBEAT_MS         = 60000  # agent keepalive interval

# ── Unit registry — 新增设备只需在这里添加一条 ──────────────
# probe 策略:
#   {'type': 'digital', 'pin': N, 'pull': 'up'|'down', 'active': 0|1}
#     pull_down + active=1 → 空引脚被拉低(0)，接了传感器(输出HIGH)读到1
#     pull_up  + active=0 → 空引脚被拉高(1)，接了传感器(输出LOW) 读到0
#   {'type': 'adc',     'pin': N, 'min_val': X}
#     空引脚 ADC 读值接近0，接了传感器读值 > min_val
#   {'type': 'flag',    'enabled': True|False}
#     执行器或无法探测引脚时使用手动开关
UNIT_CONFIGS = {
    AGENT_LIGHT: {
        'agent_type': 'sensor',
        'name': 'ambient_light',
        'probe': {'type': 'adc', 'pin': LIGHT_SENSOR_PIN, 'min_val': 30},
        'manifest': {
            'agent_tag': 'light_level',
            'levels': ['DARK', 'DIM', 'NORMAL', 'BRIGHT'],
            'ism_states': ['SAMPLING', 'ERROR'],
        },
    },
    AGENT_IR: {
        'agent_type': 'sensor',
        'name': 'ir_presence',
        'probe': {'type': 'digital', 'pin': IR_SENSOR_PIN, 'pull': 'down', 'active': 1},
        'manifest': {
            'agent_tag': 'presence',
            'values': [True, False],
            'ism_states': ['MONITORING', 'ERROR'],
        },
    },
    AGENT_SOUND: {
        'agent_type': 'sensor',
        'name': 'sound',
        # DO 引脚为开漏输出，安静时浮空无法数字探测，改为手动标志位
        'probe': {'type': 'flag', 'enabled': True},
        'manifest': {
            'agent_tag': 'sound',
            'values': ['detected'],
        },
    },
    AGENT_LED: {
        'agent_type': 'actuator',
        'name': 'ws2812_ring',
        'probe': {'type': 'flag', 'enabled': True},
        'manifest': {
            'num_pixels': WS2812_NUM,
            'commands': ['SET_COLOR', 'SET_STATE', 'BLINK'],
            'ism_states': ['OFF', 'DIM', 'BRIGHT', 'COLOR', 'BLINK'],
            'capabilities': [
                {'action': 'SET_COLOR', 'params': ['r', 'g', 'b', 'brightness']},
                {'action': 'SET_STATE', 'params': ['state'], 'values': ['ON', 'OFF', 'BRIGHT', 'DIM']},
                {'action': 'BLINK',     'params': ['r', 'g', 'b', 'count']},
            ],
            'resource_tags': ['lighting', 'ambiance'],
        },
    },
    AGENT_BUZ: {
        'agent_type': 'actuator',
        'name': 'buzzer',
        'probe': {'type': 'flag', 'enabled': BUZZER_ENABLED},
        'manifest': {
            'commands': ['PLAY', 'STOP'],
            'ism_states': ['SILENT', 'ALERT', 'NOTIFY'],
            'capabilities': [
                {'action': 'PLAY', 'params': ['pattern'], 'values': ['NOTIFY', 'ALERT']},
                {'action': 'STOP', 'params': []},
            ],
            'resource_tags': ['alert', 'notification'],
        },
    },
}
