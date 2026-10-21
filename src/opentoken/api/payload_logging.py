"""Log each API request and response body, with encoded media redacted.

Why a raw ASGI middleware
-------------------------
Body logging has to wrap `receive`/`send` rather than read the body and hand it
on: reading `await request.body()` in a `BaseHTTPMiddleware` consumes the single
`http.request` message, so the endpoint downstream would see an empty body and
every POST would silently break. Intercepting the ASGI messages instead means the
stream is observed, never drained.

What gets logged
----------------
* JSON bodies in full, pretty-printed, with encoded media replaced by `***` —
  a 4 MB base64 image is useless in a log but its presence and size are not.
* multipart bodies as form fields plus file metadata (name, type, size). The
  file bytes are never logged.
* responses with a JSON content type. SSE streams and binary downloads are
  skipped: one is unbounded, the other is not text.

Set ``OPENTOKEN_LOG_PAYLOADS=0`` to turn this off.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from email.parser import BytesParser
from email.policy import default as email_policy
from typing import Any

from opentoken.api.reference_assets import _sniff

logger = logging.getLogger("opentoken.api.payload")

# Bodies above this are truncated in the log; a base64 image request can be tens
# of MB and the log is a debugging aid, not a data store.
_MAX_CAPTURE_BYTES = 8 * 1024 * 1024
# Unrelated strings longer than this are probably prompts; keep them readable but
# bounded so one call can't write a megabyte into the log.
_MAX_STRING_CHARS = 2000
# Anything at least this long that decodes to real media is treated as an
# encoded payload and replaced.
_MIN_ENCODED_CHARS = 64
# How much of an encoded blob stays in the log. The head identifies the format
# (PNG starts `iVBORw0KGgo`, JPEG `/9j/4AAQSkZJ`), which is what a log reader
# actually needs; the rest of the image stays out.
_ENCODED_PREFIX_CHARS = 32

_SKIP_PATHS = frozenset({"/health"})

_DATA_URI = re.compile(r"^data:([^,]*),", re.IGNORECASE)
_BASE64_ALPHABET = re.compile(r"^[A-Za-z0-9+/=_\-]+$")
# Keys that conventionally hold encoded media, even when the value happens to be
# something our sniffer does not recognise.
_ENCODED_KEYS = frozenset(
    {
        "b64_json",
        "base64",
        "b64",
        "image_base64",
        "input_reference",
        "image",
        "images",
        "image_url",
        "data",
        "file_data",
        "content",
        "audio",
        "audio_url",
    }
)


def payload_logging_enabled() -> bool:
    raw = os.environ.get("OPENTOKEN_LOG_PAYLOADS", "1")
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def redact_payload(value: Any, *, key: str = "") -> Any:
    """Return a log-safe copy of a decoded JSON payload.

    Encoded media becomes `***`; the surrounding structure, key names and byte
    counts survive so a log reader can still tell what the client sent.
    """
    if isinstance(value, dict):
        return {str(k): redact_payload(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_payload(item, key=key) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        return _redact_string(value, key=key)
    return value


def _redact_string(text: str, *, key: str) -> str:
    stripped = text.strip()
    data_uri = _DATA_URI.match(stripped)
    if data_uri:
        # Keep the header ("data:image/png;base64,") plus the leading bytes so the
        # log identifies the format, and mask only the encoded remainder.
        header = f"data:{data_uri.group(1)},"
        payload = stripped[data_uri.end() :]
        return header + _mask_encoded(payload)
    media_type = _encoded_media_type(stripped, key=key)
    if media_type is not None:
        return _mask_encoded(stripped, media_type=media_type)
    if len(text) > _MAX_STRING_CHARS:
        return text[:_MAX_STRING_CHARS] + f"… ({len(text)} chars total)"
    return text


def _mask_encoded(encoded: str, *, media_type: str | None = None) -> str:
    """Keep the head of an encoded blob, mask the rest.

    The opening characters identify the format (`iVBORw0KGgo` is PNG, `/9j/4AAQ`
    is JPEG), which is exactly what a log reader needs, while the body of the
    image stays out of the log.
    """
    compact = re.sub(r"\s+", "", encoded)
    head = compact[:_ENCODED_PREFIX_CHARS]
    suffix = f", {media_type}" if media_type else ""
    return f"{head}*** (base64{suffix}, {len(compact)} chars)"


def _encoded_media_type(text: str, *, key: str) -> str | None:
    """Return the sniffed media type if `text` is a bare base64 payload.

    Decoding and sniffing is what makes this trustworthy: a long hex id is valid
    base64 by alphabet alone, but its bytes are not an image/audio/video header.
    Keys known to hold media are redacted even when the sniffer doesn't recognise
    the bytes, because a client that sent *something* under `b64_json` meant it
    as an asset.
    """
    compact = re.sub(r"\s+", "", text)
    if len(compact) < _MIN_ENCODED_CHARS or len(compact) % 4 != 0:
        return None
    if not _BASE64_ALPHABET.match(compact):
        return None
    try:
        data = base64.b64decode(
            compact.replace("-", "+").replace("_", "/"), validate=True
        )
    except (binascii.Error, ValueError):
        return None
    if not data:
        return None
    sniffed = _sniff(data)
    if sniffed is not None:
        return sniffed if _is_media_type(sniffed) else None
    # Unrecognised bytes under an asset-named key: still hide them, since the
    # alternative is dumping a caller's file into a log.
    if key.strip().lower() in _ENCODED_KEYS:
        return "unknown"
    return None


def _is_media_type(sniffed: str) -> bool:
    return sniffed.startswith(("image/", "audio/", "video/")) or sniffed in {
        "riff",
        "image/webp",
    }


def _decode_json_body(content_type: str, body: bytes) -> Any:
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1)
    try:
        return json.loads(body.decode(charset, errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"_unparsed_body": body[:200].decode("utf-8", "replace") + "…"}


def _summarize_multipart(content_type: str, body: bytes) -> Any:
    """Log form fields and file metadata without the file bytes.

    The OpenAI SDK posts multipart whenever `input_reference` is present, so this
    is the path a reference image actually takes; seeing the field names and
    sizes is what proves the client sent what it thinks it sent.
    """
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    try:
        message = BytesParser(policy=email_policy).parsebytes(header + body)
    except Exception:
        return {"_multipart": f"<{len(body)} bytes, unparsed>"}
    if not message.is_multipart():
        return {"_multipart": f"<{len(body)} bytes>"}

    summary: dict[str, Any] = {}
    for part in message.iter_parts():
        name = str(part.get_param("name", header="content-disposition") or "").strip()
        filename = part.get_filename()
        if filename is not None:
            payload = part.get_payload(decode=True) or b""
            entry = {
                "filename": filename,
                "content_type": part.get_content_type(),
                "bytes": len(payload),
            }
            existing = summary.get(name)
            if existing is None:
                summary[name or "file"] = entry
            elif isinstance(existing, list):
                existing.append(entry)
            else:
                summary[name] = [existing, entry]
        else:
            raw = part.get_payload(decode=True) or b""
            summary[name or "field"] = _redact_string(
                raw.decode("utf-8", "replace"), key=name
            )
    return summary


def _looks_json(content_type: str) -> bool:
    return "json" in content_type.lower()


def _is_capturable_response(content_type: str) -> bool:
    lowered = content_type.lower()
    # SSE is unbounded (a chat stream can run for minutes) and binary is not text.
    if "text/event-stream" in lowered:
        return False
    return "json" in lowered


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _header_map(raw_headers) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in raw_headers
    }


class PayloadLoggingMiddleware:
    """ASGI middleware that logs request/response bodies with media redacted."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or not payload_logging_enabled():
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if path in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "").upper()
        request_headers = _header_map(scope.get("headers") or [])
        request_chunks: list[bytes] = []
        request_bytes = 0
        request_truncated = False

        async def logging_receive():
            nonlocal request_bytes, request_truncated
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"") or b""
                if chunk:
                    if request_bytes + len(chunk) <= _MAX_CAPTURE_BYTES:
                        request_chunks.append(chunk)
                    else:
                        request_truncated = True
                    request_bytes += len(chunk)
            return message

        response_meta: dict[str, Any] = {}
        response_chunks: list[bytes] = []
        response_bytes = 0
        response_truncated = False

        async def logging_send(message):
            nonlocal response_bytes, response_truncated
            if message.get("type") == "http.response.start":
                headers = _header_map(message.get("headers") or [])
                response_meta["status"] = message.get("status")
                response_meta["content_type"] = headers.get("content-type", "")
                response_meta["capture"] = _is_capturable_response(
                    response_meta["content_type"]
                )
            elif message.get("type") == "http.response.body" and response_meta.get(
                "capture"
            ):
                chunk = message.get("body", b"") or b""
                if chunk:
                    if response_bytes + len(chunk) <= _MAX_CAPTURE_BYTES:
                        response_chunks.append(chunk)
                    else:
                        response_truncated = True
                    response_bytes += len(chunk)
            await send(message)

        await self.app(scope, logging_receive, logging_send)

        self._log_request(
            method=method,
            path=path,
            headers=request_headers,
            body=b"".join(request_chunks),
            total_bytes=request_bytes,
            truncated=request_truncated,
        )
        self._log_response(
            method=method,
            path=path,
            meta=response_meta,
            body=b"".join(response_chunks),
            truncated=response_truncated,
        )

    @staticmethod
    def _log_request(
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        total_bytes: int,
        truncated: bool,
    ) -> None:
        if not body and not total_bytes:
            logger.info("api_request method=%s path=%s body=<none>", method, path)
            return
        content_type = headers.get("content-type", "")
        if _looks_json(content_type):
            payload: Any = _decode_json_body(content_type, body)
        elif "multipart/form-data" in content_type.lower():
            payload = _summarize_multipart(content_type, body)
        elif "application/x-www-form-urlencoded" in content_type.lower():
            payload = {"_form": body.decode("utf-8", "replace")}
        else:
            payload = {"_body": f"<{total_bytes} bytes of {content_type or 'unknown type'}>"}
        note = f" (truncated at {_MAX_CAPTURE_BYTES} bytes)" if truncated else ""
        logger.info(
            "api_request method=%s path=%s content_type=%s%s body=%s",
            method,
            path,
            content_type or "<none>",
            note,
            _dump(redact_payload(payload)),
        )

    @staticmethod
    def _log_response(
        *,
        method: str,
        path: str,
        meta: dict[str, Any],
        body: bytes,
        truncated: bool,
    ) -> None:
        status = meta.get("status")
        content_type = str(meta.get("content_type") or "")
        if not meta.get("capture"):
            logger.info(
                "api_response method=%s path=%s status=%s content_type=%s body=<not captured>",
                method,
                path,
                status,
                content_type or "<none>",
            )
            return
        payload: Any
        try:
            payload = json.loads(body.decode("utf-8", errors="replace")) if body else None
        except json.JSONDecodeError:
            payload = {"_unparsed_body": body[:200].decode("utf-8", "replace") + "…"}
        note = f" (truncated at {_MAX_CAPTURE_BYTES} bytes)" if truncated else ""
        logger.info(
            "api_response method=%s path=%s status=%s content_type=%s%s body=%s",
            method,
            path,
            status,
            content_type or "<none>",
            note,
            _dump(redact_payload(payload)),
        )
