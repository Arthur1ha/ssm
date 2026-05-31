# tts.py — 服务端 TTS 合成
# 职责：接收文本，调用 edge-tts 生成 MP3，返回 base64 字符串。
# 不涉及 MQTT、业务逻辑或智能体状态。

import asyncio
import base64


_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


async def _generate_async(text: str, voice: str) -> bytes:
    """调用 edge-tts 流式合成，返回完整 MP3 字节。"""
    import edge_tts
    com = edge_tts.Communicate(text, voice=voice)
    audio = b""
    async for chunk in com.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    return audio


def synthesize(text: str, voice: str = _DEFAULT_VOICE) -> str | None:
    """
    同步入口：合成文本语音，返回 base64 编码的 MP3 字符串。

    在任意普通线程中调用（不依赖已有事件循环）。
    失败时返回 None，由调用方决定是否降级。

    Args:
        text:  待合成的文本。
        voice: edge-tts 声音名称，默认中文女声 XiaoxiaoNeural。

    Returns:
        base64 字符串，或 None（合成失败）。
    """
    if not text:
        return None
    try:
        loop = asyncio.new_event_loop()
        try:
            audio_bytes = loop.run_until_complete(_generate_async(text, voice))
        finally:
            loop.close()
        return base64.b64encode(audio_bytes).decode()
    except Exception as e:
        print(f"[TTS] 合成失败: {e}")
        return None
