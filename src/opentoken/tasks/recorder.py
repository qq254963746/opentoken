"""Per-task request/response dumps for media generation.

Chat requests are easy to reproduce with curl; a Doubao media render is not. The
evidence lives in two places that neither the gateway log nor the API response
carries: the exact body the client sent (often a multi-megabyte base64 image
that the log deliberately masks), and what the browser flow actually did — which
model row it clicked, whether the reference image became a composer attachment,
which ratio/duration the chip ended up showing, and what Doubao replied.

`~/.opentoken/tasks/<taskId>.json` collects all of it for one task, written
incrementally so an in-flight or crashed render still leaves something behind.

Design notes
------------
* **Complete, not redacted.** These files exist to reproduce a bug, so base64
  payloads are kept verbatim. They are local debug artifacts (mode 0600), unlike
  the log file, which stays masked.
* **Incremental + atomic.** Every section update rewrites the file via
  `write_json_atomic`, so a reader never sees a half-written document and a
  killed gateway doesn't leave a truncated one. Rewrites are throttled because a
  request body can be tens of megabytes.
* **Context-var dispatch.** `generate_doubao_video` runs several layers below
  the route (router → provider client → browser driver) and is called from a
  worker thread. Threading a recorder parameter through all of it would touch
  every signature, so an active recorder is published on a `ContextVar` and the
  provider calls `record_event(...)`; without an active recorder those calls
  are no-ops.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from opentoken.config.paths import resolve_state_dir
from opentoken.logging_setup import current_request_id
from opentoken.storage._atomic import write_json_atomic

logger = logging.getLogger("opentoken.tasks")

# A render can poll for ten minutes; rewriting a 40MB document on every poll
# would thrash the disk, so non-final writes are rate limited.
_FLUSH_INTERVAL_SECONDS = 1.0
# Guard against a pathological poll loop turning the timeline into the file.
_MAX_TIMELINE_EVENTS = 500

_active: ContextVar[TaskRecorder | None] = ContextVar(
    "opentoken_task_recorder", default=None
)

# Task ids become file names, and they arrive from URL paths, so anything that
# could escape the tasks directory is rejected rather than sanitized.
_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Statuses that mean "this task is over". Once one lands, a later write from the
# request path (which only knows the immediate POST reply) must not downgrade it.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "rejected"})


def resolve_tasks_dir() -> Path:
    override = os.getenv("OPENTOKEN_TASKS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return resolve_state_dir() / "tasks"


def dumps_enabled() -> bool:
    """Whether task files are written. On by default; `0`/`false` disables."""
    raw = os.environ.get("OPENTOKEN_TASK_DUMPS", "1")
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _sanitize(value: Any) -> Any:
    """Make `value` JSON-safe without silently dropping evidence.

    `bytes` become a length + digest descriptor rather than raw (they would be
    invalid JSON), and `Path` becomes a string; everything else is passed
    through so an unexpected type fails loudly at dump time instead of being
    swallowed here.
    """
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        return {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "base64": _b64(data),
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item) for item in value]
    return value


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


class TaskRecorder:
    """Mutable document for one task, flushed to `<tasks_dir>/<task_id>.json`."""

    def __init__(
        self,
        task_id: str,
        *,
        kind: str,
        directory: Path | None = None,
        request_id: str | None = None,
    ) -> None:
        self.task_id = task_id
        self.kind = kind
        self.directory = directory or resolve_tasks_dir()
        self._lock = threading.Lock()
        self._started = monotonic()
        self._last_flush = 0.0
        self._document: dict[str, Any] = {
            "task_id": task_id,
            "kind": kind,
            "status": "in_progress",
            "created_at": _timestamp(),
            "updated_at": _timestamp(),
            "request_id": request_id or current_request_id(),
            "request": None,
            "normalized": None,
            "upstream": None,
            "response": None,
            "error": None,
            "timeline": [],
        }

    # -- public API -------------------------------------------------------- #
    @property
    def path(self) -> Path:
        return self.directory / f"{self.task_id}.json"

    def update(self, **sections: Any) -> None:
        """Deep-merge `sections` into the document and flush (throttled)."""
        with self._lock:
            for key, value in sections.items():
                _merge(self._document, {key: _sanitize(value)})
            self._document["updated_at"] = _timestamp()
            self._flush_locked(force=False)

    def event(self, kind: str, **fields: Any) -> None:
        """Append a timestamped step to the timeline."""
        with self._lock:
            appended = _append_event(self._document, kind, fields, self._started)
            if not appended:
                return
            self._document["updated_at"] = _timestamp()
            self._flush_locked(force=False)

    def finish(
        self,
        *,
        status: str = "completed",
        response: Any = None,
        error: Any = None,
        result: Any = None,
    ) -> None:
        """Record the outcome and write the file unconditionally."""
        with self._lock:
            # A worker thread may settle the task before the route finishes
            # writing the POST reply; the terminal status is the truthful one.
            current = self._document.get("status")
            if current in _TERMINAL_STATUSES and status not in _TERMINAL_STATUSES:
                status = current
            self._document["status"] = status
            if response is not None:
                self._document["response"] = _sanitize(response)
            if error is not None:
                self._document["error"] = _sanitize(error)
            if result is not None:
                self._document["result"] = _sanitize(result)
            self._document["updated_at"] = _timestamp()
            self._flush_locked(force=True)
        if status == "failed":
            logger.info(
                "task_dump task_id=%s path=%s status=%s error=%s",
                self.task_id,
                self.path,
                status,
                str(error)[:200],
            )

    def record_request(self, **fields: Any) -> None:
        self.update(request=fields)

    def flush(self, *, force: bool = True) -> None:
        with self._lock:
            self._flush_locked(force=force)

    # -- context plumbing -------------------------------------------------- #
    @contextlib.contextmanager
    def activate(self) -> Iterator[TaskRecorder]:
        """Publish this recorder to the current context (threads included).

        Flushes on exit: provider steps are recorded while the section is
        active, and the throttle would otherwise leave the last seconds of a
        render (often the interesting part) only in memory.
        """
        token = _active.set(self)
        try:
            yield self
        finally:
            _active.reset(token)
            self.flush(force=True)

    # -- internals --------------------------------------------------------- #
    def _flush_locked(self, *, force: bool) -> None:
        now = monotonic()
        if not force and now - self._last_flush < _FLUSH_INTERVAL_SECONDS:
            return
        self._last_flush = now
        try:
            write_json_atomic(self.path, self._document, sensitive=True)
        except (OSError, TypeError, ValueError) as exc:
            # A debug artifact must never break a render.
            logger.warning(
                "task_dump_write_failed task_id=%s path=%s error=%s",
                self.task_id,
                self.path,
                exc,
            )


def new_task_id(prefix: str = "gen") -> str:
    from uuid import uuid4

    return f"{prefix}-{uuid4().hex}"


def start_task(
    task_id: str,
    *,
    kind: str,
    request: Mapping[str, Any] | None = None,
    normalized: Mapping[str, Any] | None = None,
) -> TaskRecorder | None:
    """Create a recorder unless dumps are disabled. Never raises."""
    if not dumps_enabled():
        return None
    recorder = TaskRecorder(task_id, kind=kind)
    try:
        if request is not None:
            recorder.record_request(**dict(request))
        if normalized is not None:
            recorder.update(normalized=dict(normalized))
        recorder.flush(force=True)
    except Exception as exc:  # noqa: BLE001 - debug artifact must not block
        logger.warning("task_dump_start_failed task_id=%s error=%s", task_id, exc)
        return None
    logger.info("task_dump task_id=%s path=%s status=in_progress", task_id, recorder.path)
    return recorder


def active_recorder() -> TaskRecorder | None:
    return _active.get()


def record_event(kind: str, **fields: Any) -> None:
    """Timeline an event on the active recorder; no-op when there is none."""
    recorder = _active.get()
    if recorder is None:
        return
    recorder.event(kind, **fields)


def record_section(name: str, **fields: Any) -> None:
    """Merge `fields` into `<name>` on the active recorder; no-op without one."""
    recorder = _active.get()
    if recorder is None:
        return
    recorder.update(**{name: fields})


def _merge(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in patch.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            _merge(existing, value)
        else:
            target[key] = value


def _append_event(
    document: dict[str, Any],
    kind: str,
    fields: Mapping[str, Any],
    started: float,
) -> bool:
    timeline = document.setdefault("timeline", [])
    if len(timeline) >= _MAX_TIMELINE_EVENTS:
        if len(timeline) == _MAX_TIMELINE_EVENTS:
            timeline.append({"at_ms": round((monotonic() - started) * 1000), "event": "timeline_truncated"})
        return False
    entry: dict[str, Any] = {
        "at_ms": round((monotonic() - started) * 1000),
        "at": _timestamp(),
        "event": kind,
    }
    for key, value in fields.items():
        entry[key] = _sanitize(value)
    timeline.append(entry)
    return True


def load_task(task_id: str) -> dict[str, Any] | None:
    """Read one task dump, or None when there is no readable dump for it.

    Exists so a job id can outlive the process that created it: the API keeps
    jobs in memory, but the dump on disk still has the request, the last known
    status and (for a finished render) the URL. `task_id` comes from a URL, so
    it is validated before being joined onto a path.
    """
    if not dumps_enabled():
        return None
    if not _SAFE_TASK_ID.match(str(task_id)):
        return None
    path = resolve_tasks_dir() / f"{task_id}.json"
    try:
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def mark_task_deleted(task_id: str) -> None:
    """Tombstone a dump so a deleted job is not restored from it on the next poll.

    The file itself is kept: `DELETE /v1/videos/{id}` is a client convenience, not
    a request to discard the post-mortem trail. Best-effort, like every other dump
    write — a failure must not turn a successful delete into a 500.
    """
    if not dumps_enabled() or not _SAFE_TASK_ID.match(str(task_id)):
        return
    path = resolve_tasks_dir() / f"{task_id}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            return
        document["deleted_at"] = _timestamp()
        write_json_atomic(path, document, sensitive=True)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("task_dump_delete_mark_failed task_id=%s error=%s", task_id, exc)


def dumps(value: object, *, indent: int = 2) -> str:
    """Shared serialisation used by tests to compare documents."""
    return json.dumps(value, ensure_ascii=False, indent=indent)


__all__ = [
    "TaskRecorder",
    "active_recorder",
    "dumps",
    "dumps_enabled",
    "load_task",
    "mark_task_deleted",
    "new_task_id",
    "record_event",
    "record_section",
    "resolve_tasks_dir",
    "start_task",
]
