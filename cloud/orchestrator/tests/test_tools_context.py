from unittest.mock import MagicMock

import tools


def test_http_dispatch_sends_trusted_context_headers(monkeypatch):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "internal-secret-1234")
    response = MagicMock()
    response.is_redirect = False
    response.json.return_value = {"result": "ok"}
    post = MagicMock(return_value=response)
    monkeypatch.setattr(tools.requests, "post", post)

    tools.do_http_dispatch("/api/go2/chat", {
        "session_id": "ses-a", "actor": "visitor", "persist": False,
        "space_id": "main", "message": "你好",
    }, 10)

    headers = post.call_args.kwargs["headers"]
    assert headers == {
        "X-SSM-Service-Token": "internal-secret-1234",
        "X-SSM-Session-Id": "ses-a",
        "X-SSM-Actor": "visitor",
        "X-SSM-Persist": "false",
        "X-SSM-Space-Id": "main",
    }


def test_http_dispatch_fails_closed_without_service_token(monkeypatch):
    monkeypatch.delenv("SSM_INTERNAL_SERVICE_TOKEN", raising=False)
    with __import__("pytest").raises(RuntimeError, match="not configured"):
        tools.do_http_dispatch("/api/go2/chat", {
            "session_id": "ses-a", "actor": "visitor", "persist": False,
        }, 10)
