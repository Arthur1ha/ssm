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
WS2812_PIN       = 4    # D4  — WS2812 灯环数据线
WS2812_NUM       = 16   # 灯环像素数量
WS2812_MAX_VAL   = 100  # 单通道最大值（0-255），限制峰值电流防止过热
SOUND_SENSOR_PIN = 15   # D15 — 声音传感器 数字输入（高=检测到声音，启动后正常）
LIGHT_SENSOR_PIN = 18   # D18 — 光敏传感器 数字输入（⚠️ 无 ADC，只能读亮/暗两档）
                        #       如需渐变等级，请改接 GPIO34/36（ADC1，不受 WiFi 影响）
IR_SENSOR_PIN    = 19   # D19 — 红外传感器 数字输入（低电平=检测到）

PWM_FREQ         = 5000  # Hz for buzzer base freq

# ── 光敏传感器模式 ────────────────────────────────────────────
# D18 没有 ADC，只能数字读取 → LIGHT_DIGITAL_MODE = True
# 若改接 GPIO34/36，将此设为 False 可启用渐变等级
LIGHT_DIGITAL_MODE   = True

# 以下阈值仅在 LIGHT_DIGITAL_MODE = False（ADC 模式）时生效
LIGHT_BRIGHT_THRESH  = 3000
LIGHT_NORMAL_THRESH  = 1500
LIGHT_DIM_THRESH     = 600
LIGHT_HYSTERESIS     = 100
LIGHT_CONFIRM_COUNT  = 3

# ── Timing (milliseconds) ─────────────────────────────────────
LIGHT_SAMPLE_MS      = 2000    # light polling interval
IR_DEBOUNCE_MS       = 200    # IR presence debounce
IR_HEARTBEAT_MS      = 30000  # IR heartbeat even when no change
LOCAL_RULES_DELAY_MS = 500    # delay after MQTT connect before local rules fire
HEARTBEAT_MS         = 60000  # agent keepalive publish interval
