from __future__ import annotations

import json
import logging
import queue
import threading
from time import monotonic, time
from uuid import uuid4

import httpx
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from opentoken.api.errors import (
    classify_provider_runtime_error,
    classify_stream_error,
    openai_error_response,
)
from opentoken.api.streaming import (
    ProtocolMarkupProjector,
    chunk_visible_text,
    sse_response_headers,
    strip_tool_protocol_markup,
)
from opentoken.api.usage import (
    SYSTEM_FINGERPRINT,
    estimate_prompt_tokens,
    estimate_tokens,
)
from opentoken.gateway.normalized import normalize_chat_completions_request
from opentoken.gateway.router import get_default_router
from opentoken.logging_setup import (
    current_request_id,
    reset_request_id,
    set_request_id,
)
from opentoken.providers.base import ProviderRateLimitError
from opentoken.providers.web_tool_calling import parse_web_tool_response

router = APIRouter()

logger = logging.getLogger("opentoken.api.chat")

# Browser-backed providers can go silent for tens of seconds (e.g. a multi-step
# web search runs before any answer text is emitted). With nothing on the wire,
# OpenAI clients hit their read timeout and abort the request. We emit a keepalive
# chunk on this cadence whenever the upstream stream is idle so the connection
# stays alive until real content arrives.
_STREAM_HEARTBEAT_SECONDS = 8.0


def _iter_with_heartbeat(chunks, interval: float = _STREAM_HEARTBEAT_SECONDS):
    """Yield (kind, value) from `chunks`, injecting ("heartbeat", None) on idle.

    The upstream iterator is drained on a worker thread and handed back through a
    queue, so a slow/blocking provider stream cannot starve the SSE connection:
    if no item arrives within `interval`, we surface a heartbeat and keep waiting.
    Errors raised by the upstream iterator are re-raised here, in order."""
    q: "queue.Queue[tuple[str, object]]" = queue.Queue()
    # Threads start with an empty context, so provider log lines emitted here
    # would lose the request id. Hoist the caller's id across the thread hop.
    request_id = current_request_id()

    def _drain() -> None:
        token = set_request_id(request_id)
        try:
            for item in chunks:
                q.put(("item", item))
        except Exception as exc:  # noqa: BLE001 - propagated to the consumer below
            q.put(("error", exc))
        else:
            q.put(("done", None))
        finally:
            reset_request_id(token)

    worker = threading.Thread(target=_drain, daemon=True)
    worker.start()
    while True:
        try:
            kind, value = q.get(timeout=interval)
        except queue.Empty:
            yield ("heartbeat", None)
            continue
        if kind == "item":
            yield ("item", value)
        elif kind == "error":
            raise value  # type: ignore[misc]
        else:  # done
            return


def _run_blocking_with_heartbeat(call, interval: float = _STREAM_HEARTBEAT_SECONDS):
    """Run a blocking `call()` on a worker thread, emitting heartbeats while it runs.

    Some providers cannot stream at all — e.g. a browser adapter answering a
    request that carries `tools` must see the whole reply before it can parse the
    tool protocol, so `stream_chat` returns None and the route falls back to the
    non-stream `router.chat()`. That call can block for a minute against a browser
    provider, and with nothing on the wire clients hit their read timeout and
    abort a request that is actually progressing. Yields ("heartbeat", None)
    while waiting and finally ("item", result) or ("error", exc).
    """
    q: "queue.Queue[tuple[str, object]]" = queue.Queue()
    request_id = current_request_id()

    def _run() -> None:
        token = set_request_id(request_id)
        try:
            q.put(("item", call()))
        except Exception as exc:  # noqa: BLE001 - re-raised by the consumer
            q.put(("error", exc))
        finally:
            reset_request_id(token)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    while True:
        try:
            kind, value = q.get(timeout=interval)
        except queue.Empty:
            yield ("heartbeat", None)
            continue
        yield (kind, value)
        return


def _heartbeat_chunk(completion_id: str, created: int, model: str) -> str:
    """A content-free chunk that keeps the SSE connection warm. An empty delta is
    universally tolerated by OpenAI clients (the terminal chunk uses delta {})."""
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


@router.post("/v1/chat/completions")
def chat_completions(payload: dict[str, object]) -> dict[str, object]:
    try:
        request = normalize_chat_completions_request(payload)
    except ProviderRateLimitError as exc:
        return openai_error_response(
            status_code=429,
            message=str(exc),
            error_type="rate_limit_error",
        )
    except RuntimeError as exc:
        return openai_error_response(
            status_code=400,
            message=str(exc),
            error_type="invalid_request_error",
        )
    except ValidationError as exc:
        return openai_error_response(
            status_code=400,
            message=_format_validation_error(exc),
            error_type="invalid_request_error",
        )
    except httpx.HTTPError as exc:
        # 不要把 str(exc) 直接回给客户端 —— httpx.HTTPStatusError 的 str 形如
        # "Server error '500 ...' for url 'https://upstream/x?session=...'",
        # 会泄漏 opentoken 的内部路由 + 上游 URL（可能含 session id）。换通用
        # 文案,把详情记到日志（global_exception_handler 已经有 traceback log）。
        return openai_error_response(
            status_code=502,
            message=f"Upstream provider error ({type(exc).__name__}).",
            error_type="api_error",
        )
    if request.stream:
        completion_id = f"chatcmpl-{uuid4().hex}"
        return StreamingResponse(
            _stream_chat_completion(
                request=request,
                completion_id=completion_id,
                created=int(time()),
            ),
            headers=sse_response_headers(),
            media_type="text/event-stream",
        )
    try:
        response = get_default_router().chat(request)
    except ProviderRateLimitError as exc:
        return openai_error_response(
            status_code=429,
            message=str(exc),
            error_type="rate_limit_error",
        )
    except RuntimeError as exc:
        status_code, error_type = classify_provider_runtime_error(exc)
        return openai_error_response(
            status_code=status_code,
            message=str(exc),
            error_type=error_type,
        )
    except httpx.HTTPError as exc:
        # 不要把 str(exc) 直接回给客户端 —— httpx.HTTPStatusError 的 str 形如
        # "Server error '500 ...' for url 'https://upstream/x?session=...'",
        # 会泄漏 opentoken 的内部路由 + 上游 URL（可能含 session id）。换通用
        # 文案,把详情记到日志（global_exception_handler 已经有 traceback log）。
        return openai_error_response(
            status_code=502,
            message=f"Upstream provider error ({type(exc).__name__}).",
            error_type="api_error",
        )
    created = int(time())
    visible_content = strip_tool_protocol_markup(response.content, include_think=False) or ""
    message = _chat_message_payload(visible_content, response.tool_calls)
    # 用 normalized 后的 messages 估算 prompt_tokens —— normalize 可能展开
    # multimodal content / 注入 system / 解析 file_id 附件,raw payload 的
    # messages 会少算实际发到上游的内容。
    prompt_tokens = estimate_prompt_tokens(request.messages)
    completion_tokens = estimate_tokens(visible_content)
    return {
        "id": f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion",
        "created": created,
        "model": response.model,
        "system_fingerprint": SYSTEM_FINGERPRINT,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": response.finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _stream_chat_completion(
    *,
    request,
    completion_id: str,
    created: int,
):
    started_monotonic = monotonic()
    role_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": request.model,
        "system_fingerprint": SYSTEM_FINGERPRINT,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant"},
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(role_chunk, separators=(',', ':'))}\n\n"

    router = get_default_router()
    stream_method = getattr(router, "stream_chat", None)
    if callable(stream_method):
        try:
            content_stream = stream_method(request)
        except Exception as exc:
            yield f"data: {json.dumps(_stream_error_payload(exc), separators=(',', ':'))}\n\n"
            yield "data: [DONE]\n\n"
            return
        if content_stream is not None:
            include_think = _should_include_think_in_chat_stream(request)
            projector = ProtocolMarkupProjector(include_think=include_think)
            try:
                for kind, raw_piece in _iter_with_heartbeat(content_stream):
                    if kind == "heartbeat":
                        yield _heartbeat_chunk(completion_id, created, request.model)
                        continue
                    if not raw_piece:
                        continue
                    piece = projector.push(str(raw_piece))
                    if not piece:
                        continue
                    for visible_piece in _chunk_stream_text(piece, include_think=include_think):
                        if not visible_piece:
                            continue
                        content_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": request.model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": visible_piece},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(content_chunk, separators=(',', ':'))}\n\n"
                # After the upstream stream completes, look at the full raw text we
                # accumulated: providers emit <tool_calls>…</tool_calls> markup that the
                # projector strips from visible output. If tools were involved we still
                # need to surface them as deltas, or downstream OpenAI clients will
                # never see the model's tool invocation.
                stream_tool_calls: list[dict[str, object]] = []
                stream_finish_reason = "stop"
                try:
                    _, parsed_tool_calls, parsed_finish_reason = parse_web_tool_response(
                        projector.raw_text,
                        available_tools=request.tools if request.tools else None,
                        tool_choice=request.tool_choice,
                    )
                    if parsed_tool_calls:
                        stream_tool_calls = parsed_tool_calls
                        stream_finish_reason = parsed_finish_reason or "tool_calls"
                    elif parsed_finish_reason:
                        stream_finish_reason = parsed_finish_reason
                except Exception:
                    # Parser failures should not corrupt the stream; fall through with
                    # finish_reason=stop and no tool_calls.
                    pass

                if stream_tool_calls:
                    for tool_delta in _iter_stream_tool_call_deltas(stream_tool_calls):
                        tool_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": request.model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"tool_calls": tool_delta},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(tool_chunk, separators=(',', ':'))}\n\n"

                final_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": request.model,
                    "system_fingerprint": SYSTEM_FINGERPRINT,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": stream_finish_reason,
                        }
                    ],
                }
                yield f"data: {json.dumps(final_chunk, separators=(',', ':'))}\n\n"
                yield "data: [DONE]\n\n"
                logger.info(
                    "stream_complete model=%s finish_reason=%s content_chars=%d "
                    "tool_calls=%d elapsed_ms=%.0f",
                    request.model,
                    stream_finish_reason,
                    len(projector.raw_text or ""),
                    len(stream_tool_calls),
                    (monotonic() - started_monotonic) * 1000.0,
                )
                return
            except Exception as exc:
                logger.warning(
                    "stream_failed model=%s error=%s detail=%s",
                    request.model,
                    type(exc).__name__,
                    str(exc)[:300],
                )
                yield f"data: {json.dumps(_stream_error_payload(exc), separators=(',', ':'))}\n\n"
                yield "data: [DONE]\n\n"
                return

    try:
        response = None
        for kind, value in _run_blocking_with_heartbeat(lambda: router.chat(request)):
            if kind == "heartbeat":
                yield _heartbeat_chunk(completion_id, created, request.model)
                continue
            if kind == "error":
                raise value  # type: ignore[misc]
            response = value
        if response is None:
            raise RuntimeError("Gateway returned no chat response.")
    except Exception as exc:
        yield f"data: {json.dumps(_stream_error_payload(exc), separators=(',', ':'))}\n\n"
        yield "data: [DONE]\n\n"
        return

    if response.tool_calls:
        rendered_content = strip_tool_protocol_markup(response.content, include_think=False) or ""
        for piece in _chunk_stream_text(rendered_content):
            content_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": response.model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": piece},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(content_chunk, separators=(',', ':'))}\n\n"
        for tool_delta in _iter_stream_tool_call_deltas(response.tool_calls):
            tool_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": response.model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": tool_delta},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(tool_chunk, separators=(',', ':'))}\n\n"
    else:
        rendered_content = strip_tool_protocol_markup(response.content, include_think=False) or ""
        for piece in _chunk_stream_text(rendered_content):
            content_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": response.model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": piece},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(content_chunk, separators=(',', ':'))}\n\n"

    final_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": response.model,
        "system_fingerprint": SYSTEM_FINGERPRINT,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": response.finish_reason,
            }
        ],
    }
    yield f"data: {json.dumps(final_chunk, separators=(',', ':'))}\n\n"
    yield "data: [DONE]\n\n"


def _chat_message_payload(content: str | None, tool_calls: list[dict[str, object]]) -> dict[str, object]:
    if tool_calls:
        # OpenAI spec: when tool_calls is set, content must be null. Some clients
        # treat a non-null content alongside tool_calls as the assistant having
        # already answered, which makes them skip the function-call branch.
        return {"role": "assistant", "content": None, "tool_calls": tool_calls}
    return {"role": "assistant", "content": content}


def _format_validation_error(exc: ValidationError) -> str:
    first_error = exc.errors(include_url=False)[0]
    location = ".".join(str(part) for part in first_error.get("loc", ()))
    message = str(first_error.get("msg", "Invalid request."))
    if not location:
        return message
    return f"{location}: {message}"


def _empty_chat_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def _chunk_stream_text(
    content: str,
    *,
    max_chunk_len: int = 12,
    include_think: bool = False,
) -> list[str]:
    return chunk_visible_text(content, max_chunk_len=max_chunk_len, include_think=include_think)


def _should_include_think_in_chat_stream(request) -> bool:
    if getattr(request, "tools", None):
        return False
    model = str(getattr(request, "model", "") or "").lower()
    return any(token in model for token in ("reasoner", "thinking", "-think"))


def _iter_stream_tool_call_deltas(
    tool_calls: list[dict[str, object]],
    *,
    max_arguments_chunk_len: int = 128,
):
    for index, tool_call in enumerate(tool_calls):
        function = tool_call.get("function", {})
        if not isinstance(function, dict):
            function = {}
        arguments = function.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        metadata_delta: dict[str, object] = {
            "index": index,
            "type": str(tool_call.get("type", "")).strip() or "function",
            "function": {
                "name": str(function.get("name", "")).strip(),
                "arguments": "",
            },
        }
        call_id = str(tool_call.get("id", "")).strip()
        if call_id:
            metadata_delta["id"] = call_id
        yield [metadata_delta]
        if not arguments:
            continue
        for start in range(0, len(arguments), max_arguments_chunk_len):
            yield [
                {
                    "index": index,
                    "function": {
                        "arguments": arguments[start : start + max_arguments_chunk_len],
                    },
                }
            ]


def _stream_error_payload(exc: Exception) -> dict[str, object]:
    # classify_stream_error owns both the OpenAI error.type mapping AND the
    # leak-scrubbing of the message: str(httpx.HTTPStatusError) embeds the
    # upstream URL (+ possible session id), which the non-stream path scrubs and
    # the stream path historically did not. Chat Completions wraps it in a
    # nested "error" object.
    error_type, message = classify_stream_error(exc)
    return {
        "error": {
            "message": message,
            "type": error_type,
        }
    }
