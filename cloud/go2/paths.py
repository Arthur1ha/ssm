# cloud/go2/paths.py
"""运行期数据路径的单一来源。

所有记忆 / 性格 / 规则状态集中在 DATA_DIR 下，默认 cloud/go2/data/，
可用环境变量 GO2_DATA_DIR 覆盖（部署时指向持久化卷）。
"""
import os
from pathlib import Path

DATA_DIR = Path(
    os.getenv("GO2_DATA_DIR", str(Path(__file__).resolve().parent / "data"))
)
MEMORY_DIR = DATA_DIR / "memory"
SOUL_DIR = DATA_DIR / "soul"
RULES_FILE = DATA_DIR / "rules.json"
