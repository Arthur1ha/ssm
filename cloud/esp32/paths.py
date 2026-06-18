"""ESP32 桌面智能体运行期数据路径的单一来源。

默认 cloud/esp32/data/，可用环境变量 ESP32_DATA_DIR 覆盖（部署指向持久化卷）。
"""
import os
from pathlib import Path

DATA_DIR = Path(
    os.getenv("ESP32_DATA_DIR", str(Path(__file__).resolve().parent / "data"))
)
MEMORY_DIR = DATA_DIR / "memory"
TAUGHT_FILE = MEMORY_DIR / "taught.json"
