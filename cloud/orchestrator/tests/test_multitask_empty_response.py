"""Empty coordinator answers can be retried without replaying tool effects."""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import multitask


class SequenceModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        return next(self.responses)


def response(content, *, finish_reason="stop", output_tokens=8):
    return SimpleNamespace(content=content, tool_calls=[],
                           response_metadata={"finish_reason": finish_reason},
                           usage_metadata={"output_tokens": output_tokens})


def test_empty_response_retries_once_and_records_diagnostics(monkeypatch):
    events = []
    monkeypatch.setattr(multitask, "emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))
    model = SequenceModel(response("", finish_reason="length", output_tokens=900),
                          response('{"final":"已收到"}'))

    result = multitask._model_decision(model, "prompt", {"run_id": "run"}, time.time() + 20)

    assert result == {"final": "已收到"}
    assert model.calls == 2
    assert [(fields["attempt"], fields["retrying"], fields["finish_reason"])
            for event, fields in events if event == "model.error"] == [(1, True, "length")]
    assert [fields["usage"] for event, fields in events if event == "model.response"][0] == {
        "output_tokens": 900}


def test_second_empty_response_fails_without_more_calls(monkeypatch):
    events = []
    monkeypatch.setattr(multitask, "emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))
    model = SequenceModel(response(""), response(""))

    with pytest.raises(ValueError, match="empty_model_response_after_retry"):
        multitask._model_decision(model, "prompt", {"run_id": "run"}, time.time() + 20)

    assert model.calls == 2
    assert [fields["retrying"] for event, fields in events if event == "model.error"] == [True, False]


def test_nonempty_invalid_json_is_not_retried(monkeypatch):
    monkeypatch.setattr(multitask, "emit_agent_event", lambda *_args, **_kwargs: None)
    model = SequenceModel(response("not json"))

    with pytest.raises(ValueError):
        multitask._model_decision(model, "prompt", {"run_id": "run"}, time.time() + 20)

    assert model.calls == 1
