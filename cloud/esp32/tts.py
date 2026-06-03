import asyncio
import base64

_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


async def _generate_async(text: str, voice: str) -> bytes:
    import edge_tts
    com = edge_tts.Communicate(text, voice=voice)
    audio = b""
    async for chunk in com.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    return audio


def synthesize(text: str, voice: str = _DEFAULT_VOICE) -> str | None:
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
