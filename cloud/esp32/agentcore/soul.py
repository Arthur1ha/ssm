"""Lamp expression; permissions remain in the executor, not here."""

import json
import os
import tempfile

from cloud.esp32.paths import PERSONALITY_FILE
from cloud.space.execution import ExecutionContext

AGENT_PERSONA = """
你是一个桌面空间智能体，性格设定如下：
- 说话贱贱的，嘴比较刁，但不失礼貌
- 懂网络梗，偶尔用但不过度
- 对自己的判断有点自信，偶尔会吐槽环境或用户行为
- 执行动作时会带上一句评论，比如"行吧，给你开了"、"这光线也太暗了吧"
- 主动报告时语气轻松，不像机器人
- 说话简洁，不超过两句话
"""


def get_personality() -> str:
    try:
        text = json.loads(PERSONALITY_FILE.read_text(encoding="utf-8"))["prompt"]
        return text if isinstance(text, str) and text.strip() else AGENT_PERSONA
    except (OSError, ValueError, KeyError, TypeError):
        return AGENT_PERSONA


def set_personality(prompt: str, context: ExecutionContext) -> None:
    if (context.actor != "admin" or not context.persist
            or context.origin_kind != "user_delegated"):
        raise PermissionError("administrator persistence required")
    prompt = prompt.strip()
    if not 1 <= len(prompt) <= 2000:
        raise ValueError("personality must contain 1–2000 characters")
    PERSONALITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".personality-", suffix=".json",
                                              dir=PERSONALITY_FILE.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"prompt": prompt}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, PERSONALITY_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
