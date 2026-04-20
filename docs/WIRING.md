# ESP32 实际接线

## 引脚分配

| 组件 | ESP32 引脚 | 模式 | 注意事项 |
|------|-----------|------|---------|
| 蜂鸣器（无源）| D5 / GPIO5 | PWM 输出 | 正常可用 |
| RGB 红 | D4 / GPIO4 | PWM 输出 | 正常可用 |
| RGB 绿 | D16 / GPIO16 | PWM 输出 | WROOM 正常可用 |
| RGB 蓝 | D17 / GPIO17 | PWM 输出 | WROOM 正常可用 |
| 声音传感器 | D15 / GPIO15 | 数字输入 | 运行时正常（启动 strapping 引脚，不影响运行）|
| 光敏传感器 | D18 / GPIO18 | 数字输入 | ⚠️ **无 ADC**，只能读亮/暗两档 |
| 红外传感器 | D19 / GPIO19 | 数字输入 | 正常可用，低电平=检测到 |

---

## ⚠️ 光敏传感器限制说明

**GPIO18 没有 ADC 功能**，无法读取模拟值，只能区分"亮"和"暗"两个档位。

若传感器模块有 `AO`（模拟输出）引脚，建议改接：
```
推荐改接: GPIO34 或 GPIO36（ADC1，WiFi 开启时稳定）
改接后: config.py 中将 LIGHT_DIGITAL_MODE = False
```

目前代码在数字模式下工作，触发规则为：
- D18 = LOW → BRIGHT（有光，LDR 电阻低拉低电平）
- D18 = HIGH → DARK（无光，LDR 电阻高拉高电平）

> 如果你的模块逻辑相反，修改 `bsm.py` 中 `_tick_light_digital` 里的判断：
> ```python
> new_level = LEVEL_BRIGHT if raw == 1 else LEVEL_DARK  # 反转
> ```

---

## 接线图

### 光敏传感器模块（数字模式 D18）
```
模块 VCC → 3.3V
模块 GND → GND
模块 DO  → GPIO18
模块 AO  → 暂不接（或改接 GPIO34 启用 ADC 模式）
```

### 红外传感器模块（FC-51 等，D19）
```
模块 VCC → 3.3V
模块 GND → GND
模块 OUT → GPIO19   （低电平 = 检测到）
```

### 声音传感器模块（D15）
```
模块 VCC → 3.3V
模块 GND → GND
模块 DO  → GPIO15   （高电平 = 检测到声音）
```
> 模块上的电位器调节灵敏度（顺时针 = 阈值降低 = 更容易触发）

### RGB LED 共阴极（D4/D16/D17）
```
GPIO4  → 220Ω → R 脚
GPIO16 → 220Ω → G 脚
GPIO17 → 220Ω → B 脚
GND    →        公共阴极
```

### 无源蜂鸣器（D5）
```
GPIO5 → 蜂鸣器正极
GND   → 蜂鸣器负极
```
> **无源蜂鸣器**需要 PWM 驱动，代码用不同频率产生音调。  
> 若是**有源蜂鸣器**，只需高低电平，需修改 `bsm.py` 中 buzzer 相关代码。

---

## 升级路径：光敏传感器改为 ADC 模式

1. 将传感器 `AO` 引脚改接 **GPIO34**
2. 编辑 `config.py`：
   ```python
   LIGHT_SENSOR_PIN = 34
   LIGHT_DIGITAL_MODE = False
   ```
3. 重新上传 `config.py`，重启 ESP32
4. 获得 4 档光照等级：`DARK / DIM / NORMAL / BRIGHT`
