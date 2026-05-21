import os
import json
import queue
import time
import threading

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage


class DeskAgent:
    def __init__(self, shared_state, publish_task_fn, llm=None):
        self._shared_state = shared_state
        self._publish_task = publish_task_fn
        self._llm = llm if llm is not None else self._make_llm()
        self._event_queue: queue.Queue = queue.Queue()
        self._belief_history: list[dict] = []
        self._cooldown: dict[str, float] = {}

    def _make_llm(self):
        model_list_str = os.getenv("MODEL_LIST", os.getenv("MODEL", ""))
        models = [m.strip() for m in model_list_str.split(",") if m.strip()]
        if not models:
            models = ["doubao-seed-2-0-lite-260215"]
        base_kwargs = dict(
            base_url=os.getenv("OPENAI_BASE_URL"),
            api_key=os.getenv("OPENAI_API_KEY"),
            temperature=0,
            timeout=30,
        )
        llms = [ChatOpenAI(model=m, **base_kwargs) for m in models]
        return llms[0].with_fallbacks(llms[1:]) if len(llms) > 1 else llms[0]

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True, name="DeskAgent")
        t.start()

    def push_sensor_event(self, unit_id: str, payload: dict):
        self._event_queue.put({"unit_id": unit_id, "payload": payload})

    def _sense(self) -> dict | None:
        snap = self._shared_state.sensor_snapshot()

        light_data = None
        for unit_id, data in snap.items():
            if unit_id.endswith("_light"):
                light_data = data.get("state") or data.get("event")
                break

        if not light_data:
            return None

        level = light_data.get("level", "NORMAL")
        lux = light_data.get("lux", 0)

        sound_detected = False
        sound_recent = False
        now = time.time()
        for unit_id, data in snap.items():
            if unit_id.endswith("_sound"):
                event = data.get("event", {})
                if event:
                    event_ts = event.get("ts", 0)
                    sound_recent = (now - event_ts) < 5
                    sound_detected = sound_recent
                break

        return {
            "light_level": level,
            "light_lux": lux,
            "sound_detected": sound_detected,
            "sound_recent": sound_recent,
        }

    def _reason(self, sense_data: dict) -> dict | None:
        pass  # Task 4

    def _act(self, belief: dict):
        pass  # Task 5

    def _loop(self):
        pass  # Task 6
