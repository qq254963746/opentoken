"""Central logging configuration for the gateway.

Why this module exists
----------------------
The API layer has always logged `request_complete request_id=... elapsed_ms=...`,
but nothing ever configured Python logging. The root logger therefore stayed at
its default (WARNING, no handlers), so every `logger.info` call was silently
dropped: `~/.opentoken/logs/gateway.log` held zero request lines and the only
trace of a request was uvicorn's access line — no timing, no request id, no
provider. Debugging "the request hung for 120s" then required ad-hoc scripts.

`configure_logging()` fixes that: one root handler writing to stderr *and* to a
rotating file under the state dir, with a `request_id` field stamped onto every
record so API, gateway and provider lines can be correlated.
"""

from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar, Token
from logging.handlers import RotatingFileHandler
from pathlib import Path

from opentoken.config.paths import resolve_state_dir

LOG_FORMAT = (
    "%(asctime)s %(levelname)-5s [%(request_id)s] %(name)s: %(message)s"
)
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5

# Filesystem-safe stand-in for "no request in flight" (uvicorn startup lines,
# background worker threads that were not handed a request id).
NO_REQUEST_ID = "-"

_request_id: ContextVar[str] = ContextVar("opentoken_request_id", default=NO_REQUEST_ID)


def current_request_id() -> str:
    return _request_id.get()


def set_request_id(request_id: str) -> Token[str]:
    """Bind *request_id* to the current context. Returns a token for resetting.

    Context vars are copied into `run_in_threadpool` work (which is how FastAPI
    runs sync endpoints), so provider logs emitted from the request's thread
    inherit the id automatically. Threads we spawn ourselves do not inherit it —
    those call this explicitly with `current_request_id()` captured beforehand.
    """
    return _request_id.set(request_id or NO_REQUEST_ID)


def reset_request_id(token: Token[str]) -> None:
    try:
        _request_id.reset(token)
    except ValueError:
        # Token created in a different context (e.g. reset after a thread hop).
        _request_id.set(NO_REQUEST_ID)


class RequestIdFilter(logging.Filter):
    """Guarantee every record carries a `request_id` the formatter can render."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = current_request_id()
        return True


def resolve_log_file() -> Path | None:
    """Path of the rotating log file, or None when file logging is disabled.

    `OPENTOKEN_LOG_FILE=""` / `"none"` / `"-"` disables it (useful for tests and
    for running in the foreground when stdout is already captured).
    """
    override = os.getenv("OPENTOKEN_LOG_FILE")
    if override is not None:
        value = override.strip()
        if value.lower() in {"", "none", "-", "off"}:
            return None
        return Path(value).expanduser()
    return resolve_state_dir() / "logs" / "gateway.log"


def configure_logging(
    level: str | int | None = None,
    *,
    log_file: Path | None | str = "auto",
    to_stderr: bool = True,
    force: bool = True,
) -> Path | None:
    """Install root handlers. Idempotent; returns the file path in use.

    Existing root handlers are removed when *force* is set so repeated calls (the
    CLI's `start`, tests, the `create_app` factory) don't stack handlers and
    duplicate every line.
    """
    resolved_level = _coerce_level(level if level is not None else os.getenv("OPENTOKEN_LOG_LEVEL"))
    if log_file == "auto":
        target_file = resolve_log_file()
    elif log_file is None:
        target_file = None
    else:
        target_file = Path(log_file).expanduser()

    root = logging.getLogger()
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    request_id_filter = RequestIdFilter()
    handlers: list[logging.Handler] = []

    if to_stderr:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(request_id_filter)
        handlers.append(stream_handler)

    if target_file is not None:
        try:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                target_file,
                maxBytes=DEFAULT_LOG_MAX_BYTES,
                backupCount=DEFAULT_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        except OSError as exc:
            # An unwritable log path must not stop the gateway from serving.
            logging.getLogger(__name__).warning(
                "log_file_unavailable path=%s error=%s", target_file, exc
            )
            target_file = None
        else:
            file_handler.setFormatter(formatter)
            file_handler.addFilter(request_id_filter)
            handlers.append(file_handler)

    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(resolved_level)

    # Uvicorn's own loggers default to propagate=False with their own stream
    # handlers, which would bypass the file handler. Route them through root so
    # startup and access lines land in the same place as application logs.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = []
        uv_logger.propagate = True
        uv_logger.setLevel(resolved_level)

    for name in ("httpx", "httpcore", "asyncio"):
        # These emit a line per connection at INFO, which drowns real requests.
        logging.getLogger(name).setLevel(logging.WARNING)

    return target_file


def _coerce_level(value: str | int | None) -> int:
    if value is None or value == "":
        return logging.INFO
    if isinstance(value, int):
        return value
    text = str(value).strip().upper()
    named = logging.getLevelName(text)
    if isinstance(named, int):
        return named
    return logging.INFO
