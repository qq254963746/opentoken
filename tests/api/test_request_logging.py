import logging

from fastapi.testclient import TestClient

from opentoken.api.app import create_app
from opentoken.logging_setup import configure_logging
from opentoken.providers.base import ChatResponse


class _FakeRouter:
    def chat(self, request):
        return ChatResponse(model=request.model, content="answer")

    def stream_chat(self, request):
        return iter(["answer"])


def _log_lines(path) -> str:
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            handler.flush()
    return path.read_text(encoding="utf-8")


def test_request_complete_line_reaches_the_log_file(tmp_path, monkeypatch) -> None:
    """Regression: `request_complete` used to be logged into the void because
    nothing configured Python logging (root sat at WARNING, no handlers)."""
    log_file = tmp_path / "gateway.log"
    configure_logging("INFO", log_file=log_file, to_stderr=False)
    monkeypatch.setattr(
        "opentoken.api.routes.chat.get_default_router", lambda: _FakeRouter()
    )
    client = TestClient(create_app())

    response = client.post(
        "/v1/chat/completions",
        json={"model": "doubao/doubao-seed-2.0", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    contents = _log_lines(log_file)
    request_id = response.headers["X-Request-Id"]
    assert "request_complete" in contents
    assert f"request_id={request_id}" in contents
    assert "path=/v1/chat/completions" in contents
    assert "status=200" in contents
    # elapsed_ms is the field that makes a slow request diagnosable.
    assert "elapsed_ms=" in contents


def test_client_supplied_request_id_is_reused_in_logs(tmp_path, monkeypatch) -> None:
    log_file = tmp_path / "gateway.log"
    configure_logging("INFO", log_file=log_file, to_stderr=False)
    monkeypatch.setattr(
        "opentoken.api.routes.chat.get_default_router", lambda: _FakeRouter()
    )
    client = TestClient(create_app())

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Request-Id": "caller-supplied-id"},
        json={"model": "doubao/doubao-seed-2.0", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.headers["X-Request-Id"] == "caller-supplied-id"
    contents = _log_lines(log_file)
    assert "[caller-supplied-id]" in contents
