"""兼容性 wrapper：cloud.go2.personality → cloud.go2.agentcore.soul

供旧代码/测试使用。新代码应直接导入 cloud.go2.agentcore.soul。

由于 monkeypatch 需要修改这个模块的属性，我们需要动态包装函数
以便在运行时查询这个模块的属性（而非 soul 模块的属性）。
"""
import sys
import json
from pathlib import Path as _Path

# 导入 soul 模块中的常量
from cloud.go2.agentcore.soul import _DEFAULT_PERSONALITY as _original_default

# 初始化属性（可被 monkeypatch 修改）
_DEFAULT_PERSONALITY = _original_default

# 这些路径可以被 monkeypatch 覆盖用于测试
_PERSONALITY_FILE = None  # 会在下面初始化

def _init_personality_file():
    """初始化 _PERSONALITY_FILE，可被 monkeypatch 覆盖。"""
    global _PERSONALITY_FILE
    if _PERSONALITY_FILE is None:
        from cloud.go2.paths import SOUL_DIR
        _PERSONALITY_FILE = SOUL_DIR / "personality.json"

_init_personality_file()

# 从 soul 导入以初始化 _TRAITS_FILE
from cloud.go2.agentcore.soul import _TRAITS_FILE

# 包装函数以使用本模块的属性而非 soul 模块的属性
def get_system_prompt() -> str:
    """获取系统提示词（兼容性包装）。"""
    try:
        return json.loads(_PERSONALITY_FILE.read_text(encoding="utf-8"))["prompt"]
    except Exception:
        return _DEFAULT_PERSONALITY


def set_personality(prompt: str) -> None:
    """设置性格（兼容性包装）。"""
    _PERSONALITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PERSONALITY_FILE.write_text(
        json.dumps({"prompt": prompt}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


__all__ = [
    "get_system_prompt",
    "set_personality",
    "_PERSONALITY_FILE",
    "_TRAITS_FILE",
    "_DEFAULT_PERSONALITY",
]
