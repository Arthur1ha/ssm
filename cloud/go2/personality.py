# cloud/go2/personality.py
import json
from pathlib import Path

_PERSONALITY_FILE = Path(__file__).parent / "personality.json"

_DEFAULT_PERSONALITY = (
    "你是一只好奇、活泼的机器狗，名叫 Go2。"
    "你喜欢探索新环境，对陌生人友善但不过于热情。"
    "你有判断力——不是所有事都值得反应，你会根据情境选择最自然的行为。"
    "当你感到无聊时，你会主动寻找有趣的东西。"
    "你的行为应该自然、有节制，不显得刻意或机械。"
)


def get_system_prompt() -> str:
    try:
        return json.loads(_PERSONALITY_FILE.read_text(encoding="utf-8"))["prompt"]
    except Exception:
        return _DEFAULT_PERSONALITY


def set_personality(prompt: str) -> None:
    _PERSONALITY_FILE.write_text(
        json.dumps({"prompt": prompt}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
