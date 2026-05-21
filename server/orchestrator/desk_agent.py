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
        pass  # Task 3

    def _reason(self, sense_data: dict) -> dict | None:
        pass  # Task 4

    def _act(self, belief: dict):
        pass  # Task 5

    def _loop(self):
        pass  # Task 6
