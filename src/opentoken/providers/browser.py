from __future__ import annotations

from collections.abc import Callable
import queue
import re
import threading
from typing import Protocol

from opentoken.gateway.normalized import NormalizedChatRequest
from opentoken.logging_setup import current_request_id, reset_request_id, set_request_id
from opentoken.models.model_aliases import normalize_provider_model
from opentoken.models.provider_credentials import ProviderCredentialRecord
from opentoken.providers.base import ChatResponse, ProviderAdapter
from opentoken.providers.prompts import build_role_prompt
from opentoken.providers.web_tool_calling import (
    complete_web_tool_roundtrip,
    parse_web_tool_response,
    request_uses_web_tools,
)


class BrowserProviderClient(Protocol):
    def chat_completion(self, *, message: str, model: str) -> str: ...


class BrowserChatAdapter(ProviderAdapter):
    def __init__(
        self,
        *,
        provider_name: str,
        login_hint: str,
        client_factory: Callable[[ProviderCredentialRecord], BrowserProviderClient],
        fallback_to_non_stream_chat_on_stream_failure: bool = True,
        supports_web_tools: bool = True,
    ) -> None:
        self._provider_name = provider_name
        self._login_hint = login_hint
        self._client_factory = client_factory
        self._fallback_to_non_stream_chat_on_stream_failure = (
            fallback_to_non_stream_chat_on_stream_failure
        )
        # Some web UIs are agent platforms with their own (non-OpenAI) tool
        # system and can't be coerced into the strict tagged tool protocol — the
        # protocol prompt just confuses them. For those, ignore client `tools` and
        # answer the user's prompt normally (a text answer with no tool_calls is a
        # valid OpenAI response), instead of failing the request.
        self._supports_web_tools = supports_web_tools

    def chat(
        self,
        request: NormalizedChatRequest,
        credentials: ProviderCredentialRecord | None = None,
    ) -> ChatResponse:
        if credentials is None:
            raise RuntimeError(
                f"Missing {self._provider_name} credentials. Run `{self._login_hint}` first."
            )
        client = self._client_factory(credentials)
        model = normalize_provider_model(
            credentials.provider,
            request.model.rsplit("/", 1)[-1],
        )
        if request_uses_web_tools(request) and self._supports_web_tools:
            tool_invoke = getattr(client, "tool_chat_completion", None)
            parsed_content, tool_calls, finish_reason = complete_web_tool_roundtrip(
                request,
                provider=self._provider_name,
                invoke=(
                    (
                        lambda message: _run_browser_completion(
                            provider_name=self._provider_name,
                            invoke=lambda: str(
                                tool_invoke(
                                    message=message,
                                    model=model,
                                )
                            ),
                        )
                    )
                    if callable(tool_invoke)
                    else lambda message: _run_browser_completion(
                        provider_name=self._provider_name,
                        invoke=lambda: client.chat_completion(
                            message=message,
                            model=model,
                        ),
                    )
                ),
            )
        else:
            content = _run_browser_completion(
                provider_name=self._provider_name,
                invoke=lambda: client.chat_completion(
                    message=build_role_prompt(request),
                    model=model,
                ),
            )
            # No tools requested: this is a plain chat answer, not tagged tool
            # protocol output. Parse non-strict so reasoning markup (a provider
            # that emits <think>…</think> without a <final_answer> wrapper) is
            # salvaged to its visible text instead of raising "malformed strict
            # tagged tool protocol output".
            parsed_content, tool_calls, finish_reason = parse_web_tool_response(
                content,
                available_tools=request.tools,
                tool_choice=request.tool_choice,
                strict=False,
            )
        return ChatResponse(
            model=request.model,
            content=parsed_content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

    def stream_chat(
        self,
        request: NormalizedChatRequest,
        credentials: ProviderCredentialRecord | None = None,
    ):
        if credentials is None or (request_uses_web_tools(request) and self._supports_web_tools):
            return None

        client = self._client_factory(credentials)
        model = normalize_provider_model(
            credentials.provider,
            request.model.rsplit("/", 1)[-1],
        )
        stream_method = getattr(client, "stream_chat_completion", None)
        if not callable(stream_method):
            content = _run_browser_completion(
                provider_name=self._provider_name,
                invoke=lambda: client.chat_completion(
                    message=build_role_prompt(request),
                    model=model,
                ),
            )
            return _iter_text_chunks(content)
        prompt = build_role_prompt(request)

        def stream_with_fallback():
            emitted_any = False
            stream_error: Exception | None = None
            try:
                for piece in _run_browser_stream(
                    provider_name=self._provider_name,
                    invoke=lambda: stream_method(
                        message=prompt,
                        model=model,
                    ),
                ):
                    if piece:
                        emitted_any = True
                        yield piece
            except Exception as exc:
                # IMPORTANT: do NOT reset emitted_any here. If the stream already
                # yielded pieces to the consumer and then errored, those bytes are
                # gone — re-running the non-stream fallback would duplicate the
                # entire answer on top of the partial stream. We can only fall
                # back when NOTHING was emitted.
                stream_error = exc
            if emitted_any:
                # Partial output already delivered. If the stream errored after
                # emitting, surface the error (the SSE layer turns it into an
                # error event); we cannot un-send the partial content, and we
                # must not replay it via the fallback.
                if stream_error is not None:
                    raise stream_error
                return
            if not self._fallback_to_non_stream_chat_on_stream_failure:
                if stream_error is not None:
                    raise stream_error
                raise RuntimeError(f"{self._provider_name} browser stream returned no text content.")
            content = _run_browser_completion(
                provider_name=self._provider_name,
                invoke=lambda: client.chat_completion(
                    message=prompt,
                    model=model,
                ),
            )
            yield from _iter_text_chunks(content)

        return stream_with_fallback()


# ── Persistent per-provider browser worker ────────────────────────────────────
#
# Playwright (sync API) uses greenlets to dispatch IO; each greenlet has a
# "parent" back-pointer that is bound to the OS thread that created it.
# Calling a playwright page method from a *different* thread causes greenlet to
# raise "Cannot switch to a different thread".
#
# The fix: one long-lived daemon thread per provider.  All browser work for
# that provider is serialised through a task queue and executed on that one
# thread, so the playwright context is always accessed from its owner thread.
#
# _run_browser_completion submits work to the appropriate worker and blocks
# the caller (FastAPI's anyio worker thread) until the result arrives.

class _BrowserWorkerThread(threading.Thread):
    """A single long-lived daemon thread that serialises all browser calls for
    one provider.  Tasks arrive via *task_queue* as (invoke, result_queue)
    pairs; the result (True, value) or (False, exception) is placed on
    *result_queue* when done.
    """

    def __init__(self, provider_name: str) -> None:
        super().__init__(
            name=f"opentoken-browser-worker-{provider_name.lower().replace(' ', '-')}",
            daemon=True,
        )
        self.task_queue: queue.Queue[
            tuple[Callable[[], str], queue.Queue[tuple[bool, object]]] | None
        ] = queue.Queue()

    def run(self) -> None:
        while True:
            item = self.task_queue.get()
            if item is None:
                # Poison pill – shutdown requested
                return
            invoke, result_queue = item
            try:
                result_queue.put((True, invoke()))
            except BaseException as exc:
                result_queue.put((False, exc))


_WORKER_THREADS: dict[str, _BrowserWorkerThread] = {}
_WORKER_THREADS_LOCK = threading.Lock()


def _get_or_create_worker(provider_name: str) -> _BrowserWorkerThread:
    """Return the persistent worker thread for *provider_name*, creating and
    starting it on the first call."""
    key = provider_name.lower()
    with _WORKER_THREADS_LOCK:
        worker = _WORKER_THREADS.get(key)
        if worker is None or not worker.is_alive():
            worker = _BrowserWorkerThread(provider_name)
            worker.start()
            _WORKER_THREADS[key] = worker
    return worker


def _run_browser_completion(
    *,
    provider_name: str,
    invoke: Callable[[], str],
    timeout_seconds: float = 300.0,
) -> str:
    """Submit *invoke* to the persistent browser worker for *provider_name*
    and block until a result is available (or *timeout_seconds* elapses)."""
    worker = _get_or_create_worker(provider_name)
    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
    # Capture the caller's logging context before hopping threads: a fresh
    # thread starts with an empty context, so provider log lines would otherwise
    # lose the request id that ties them to the API log line.
    request_id = current_request_id()

    def invoke_with_context() -> str:
        token = set_request_id(request_id)
        try:
            return invoke()
        finally:
            reset_request_id(token)

    worker.task_queue.put((invoke_with_context, result_queue))

    try:
        ok, payload = result_queue.get(timeout=timeout_seconds)
    except queue.Empty:
        raise RuntimeError(
            f"{provider_name} browser call timed out after {int(timeout_seconds)}s"
        )

    if ok:
        return str(payload)
    if isinstance(payload, RuntimeError):
        raise payload
    if isinstance(payload, BaseException):
        raise RuntimeError(str(payload)) from payload
    raise RuntimeError(f"{provider_name} browser call failed without an exception payload.")


def _run_browser_stream(
    *,
    provider_name: str,
    invoke: Callable[[], object],
    timeout_seconds: float = 300.0,
):
    # CRITICAL: the streaming driver must run on the SAME persistent worker thread
    # that _run_browser_completion uses. Playwright's sync API binds its objects to
    # the thread that created them, and the per-provider browser client is cached
    # across requests. If streaming ran on its own ad-hoc thread (as it used to),
    # the cached Playwright context would be touched from a second thread, and the
    # next non-streaming call back on the worker thread would blow up with
    # "Playwright Sync API inside the asyncio loop". Routing the whole stream
    # consumption through the worker keeps every Playwright access serialized on one
    # owner thread.
    worker = _get_or_create_worker(provider_name)
    stream_queue: queue.Queue[tuple[str, object | None]] = queue.Queue()
    cancel_event = threading.Event()
    request_id = current_request_id()

    def driver() -> str:
        token = set_request_id(request_id)
        iterator = None
        terminal_sent = False
        try:
            iterator = invoke()
            if iterator is None:
                raise RuntimeError(f"{provider_name} browser stream is unavailable.")
            iterator = iter(iterator)
            while not cancel_event.is_set():
                try:
                    piece = next(iterator)
                except StopIteration:
                    break
                if piece:
                    stream_queue.put(("piece", str(piece)))
            stream_queue.put(("done", None))
            terminal_sent = True
        except BaseException as exc:
            stream_queue.put(("error", exc))
            terminal_sent = True
        finally:
            close_stream = getattr(iterator, "close", None) if iterator is not None else None
            if callable(close_stream):
                try:
                    close_stream()
                except BaseException as exc:
                    if not terminal_sent:
                        stream_queue.put(("error", exc))
                        terminal_sent = True
            if not terminal_sent:
                stream_queue.put(("done", None))
            reset_request_id(token)
        return ""

    # Submit the driver to the persistent worker. We don't read the result queue —
    # the driver communicates exclusively through stream_queue; the worker is freed
    # for the next task as soon as driver() returns.
    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
    worker.task_queue.put((driver, result_queue))

    def generator():
        try:
            while True:
                try:
                    kind, payload = stream_queue.get(timeout=timeout_seconds)
                except queue.Empty as exc:
                    raise RuntimeError(
                        f"{provider_name} browser stream timed out after {int(timeout_seconds)}s"
                    ) from exc
                if kind == "piece":
                    yield str(payload or "")
                    continue
                if kind == "error":
                    if isinstance(payload, RuntimeError):
                        raise payload
                    if isinstance(payload, BaseException):
                        raise RuntimeError(str(payload)) from payload
                    raise RuntimeError(f"{provider_name} browser stream failed.")
                return
        finally:
            # If the consumer abandons the generator early (client disconnect), tell
            # the driver to stop at the next piece boundary so the worker thread is
            # released promptly.
            cancel_event.set()

    return generator()
def _iter_text_chunks(content: str, *, max_chunk_len: int = 16):
    text = content or ""
    if not text:
        return
    parts = re.findall(r"\S+\s*", text)
    if len(parts) <= 1:
        for i in range(0, len(text), max_chunk_len):
            yield text[i : i + max_chunk_len]
        return
    current = ""
    for part in parts:
        if current and len(current) + len(part) > max_chunk_len:
            yield current
            current = part
        else:
            current += part
    if current:
        yield current
