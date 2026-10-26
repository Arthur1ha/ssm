"""Go2's bounded view of the shared MQTT space snapshot."""

import json

from cloud.space.context import get_space_context


def prompt_space_context() -> str:
    from cloud.api.main import _esp32_state
    return json.dumps(get_space_context("go2", state=_esp32_state),
                      ensure_ascii=False, default=str)[:5000]
