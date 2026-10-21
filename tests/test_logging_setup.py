import logging

import pytest

from opentoken.logging_setup import (
    NO_REQUEST_ID,
    RequestIdFilter,
    configure_logging,
    current_request_id,
    reset_request_id,
    resolve_log_file,
    set_request_id,
)


@pytest.fixture(autouse=True)
def restore_root_logging():
    """configure_logging mutates the global root logger; put it back."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    for handler in original_handlers:
        root.addHandler(handler)
    root.setLevel(original_level)


def test_configure_logging_writes_request_lines_to_the_file(tmp_path) -> None:
    log_file = tmp_path / "logs" / "gateway.log"

    assert configure_logging("INFO", log_file=log_file, to_stderr=False) == log_file

    logging.getLogger("opentoken.api").info("request_complete request_id=abc status=200")
    for handler in logging.getLogger().handlers:
        handler.flush()

    contents = log_file.read_text(encoding="utf-8")
    assert "request_complete request_id=abc" in contents
    # Format carries timestamp, level, request id and logger name so a line can
    # be traced back to a client request.
    assert "INFO" in contents
    assert "opentoken.api" in contents


def test_configure_logging_is_idempotent(tmp_path) -> None:
    log_file = tmp_path / "gateway.log"

    configure_logging("INFO", log_file=log_file, to_stderr=False)
    first = len(logging.getLogger().handlers)
    configure_logging("INFO", log_file=log_file, to_stderr=False)

    # Repeated calls must not stack handlers, which would duplicate every line.
    assert len(logging.getLogger().handlers) == first == 1


def test_configure_logging_level_controls_verbosity(tmp_path) -> None:
    log_file = tmp_path / "gateway.log"

    configure_logging("WARNING", log_file=log_file, to_stderr=False)
    logging.getLogger("opentoken.test").info("should be dropped")
    logging.getLogger("opentoken.test").warning("should be kept")

    contents = log_file.read_text(encoding="utf-8")
    assert "should be dropped" not in contents
    assert "should be kept" in contents


def test_log_file_can_be_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENTOKEN_LOG_FILE", "none")
    assert resolve_log_file() is None
    assert configure_logging("INFO", to_stderr=False) is None


def test_log_file_env_override(tmp_path, monkeypatch) -> None:
    target = tmp_path / "custom.log"
    monkeypatch.setenv("OPENTOKEN_LOG_FILE", str(target))
    assert resolve_log_file() == target


def test_request_id_is_stamped_on_every_record(tmp_path) -> None:
    log_file = tmp_path / "gateway.log"
    configure_logging("INFO", log_file=log_file, to_stderr=False)

    token = set_request_id("req-123")
    logging.getLogger("opentoken.providers.browser").info("doubao_dom_first_token")
    reset_request_id(token)
    logging.getLogger("opentoken.providers.browser").info("outside_a_request")

    lines = log_file.read_text(encoding="utf-8").splitlines()

    assert any("[req-123]" in line and "doubao_dom_first_token" in line for line in lines)
    assert any(f"[{NO_REQUEST_ID}]" in line and "outside_a_request" in line for line in lines)


def test_current_request_id_defaults_to_placeholder() -> None:
    assert current_request_id() == NO_REQUEST_ID


def test_request_id_filter_fills_missing_attribute() -> None:
    record = logging.LogRecord(
        name="opentoken.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    token = set_request_id("req-abc")
    try:
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "req-abc"
    finally:
        reset_request_id(token)


def test_uvicorn_loggers_are_routed_through_the_root_handlers(tmp_path) -> None:
    """Uvicorn defaults to its own stream handlers with propagate=False, which
    would keep access lines out of the log file entirely."""
    configure_logging("INFO", log_file=tmp_path / "gateway.log", to_stderr=False)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        assert uv_logger.propagate is True
        assert uv_logger.handlers == []
