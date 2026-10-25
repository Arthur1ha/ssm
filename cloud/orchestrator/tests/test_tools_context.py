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


def test_peer_http_dispatch_carries_origin_and_deadline(monkeypatch):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "internal-secret-1234")
    response = MagicMock()
    response.is_redirect = False
    response.json.return_value = {"result": "ok"}
    post = MagicMock(return_value=response)
    monkeypatch.setattr(tools.requests, "post", post)

    tools.do_http_dispatch("/api/go2/chat", {
        "session_id": "user-a", "actor": "visitor", "persist": False,
        "space_id": "main", "message": "打招呼", "origin_kind": "user_delegated",
        "origin_session_id": "user-a", "expires_at": 1234.0,
    }, 5)

    headers = post.call_args.kwargs["headers"]
    assert headers["X-SSM-Origin-Kind"] == "user_delegated"
    assert headers["X-SSM-Expires-At"] == "1234"


def test_http_dispatch_fails_closed_without_service_token(monkeypatch):
    monkeypatch.delenv("SSM_INTERNAL_SERVICE_TOKEN", raising=False)
    with __import__("pytest").raises(RuntimeError, match="not configured"):
        tools.do_http_dispatch("/api/go2/chat", {
            "session_id": "ses-a", "actor": "visitor", "persist": False,
        }, 10)


def test_http_dispatch_polls_existing_task_after_202(monkeypatch):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "internal-secret-1234")
    accepted = MagicMock()
    accepted.is_redirect = False
    accepted.json.return_value = {"result": "executing", "task_id": "request_1"}
    done = MagicMock()
    done.is_redirect = False
    done.json.return_value = {"result": "ok", "task_id": "request_1"}
    monkeypatch.setattr(tools.requests, "post", MagicMock(return_value=accepted))
    get = MagicMock(return_value=done)
    monkeypatch.setattr(tools.requests, "get", get)

    result = tools.do_http_dispatch("/api/go2/chat", {
        "session_id": "ses-a", "actor": "visitor", "persist": False,
        "task_id": "request_1", "message": "前进两秒",
    }, 5)
    assert result["result"] == "ok"
    assert get.call_args.args[0].endswith("/api/go2/tasks/request_1")


def test_http_dispatch_queries_after_post_timeout_without_resending(monkeypatch):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "internal-secret-1234")
    post = MagicMock(side_effect=tools.requests.exceptions.Timeout())
    monkeypatch.setattr(tools.requests, "post", post)
    done = MagicMock()
    done.is_redirect = False
    done.json.return_value = {"result": "accepted", "task_id": "request_1"}
    monkeypatch.setattr(tools.requests, "get", MagicMock(return_value=done))

    result = tools.do_http_dispatch("/api/go2/chat", {
        "session_id": "ses-a", "actor": "visitor", "persist": False,
        "task_id": "request_1", "message": "站起来",
    }, 5)
    assert result["result"] == "accepted"
    post.assert_called_once()
