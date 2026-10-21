"""Payload logging: full bodies, but encoded media masked after a short prefix."""
from __future__ import annotations

import base64
import logging

import pytest
from fastapi.testclient import TestClient

from opentoken.api.app import create_app
from opentoken.api.payload_logging import redact_payload


def _png(width: int = 16, height: int = 16) -> bytes:
    import struct
    import zlib

    raw = b"".join(b"\x00" + bytes([200, 90, 40] * width) for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def test_bare_base64_keeps_its_prefix_and_masks_the_rest() -> None:
    """The head identifies the format; the body of the image must not be logged.

    A PNG always starts with `iVBORw0KGgo`, so keeping 32 chars is enough for a
    log reader to recognise it while the payload itself stays out.
    """
    encoded = base64.b64encode(_png()).decode()

    masked = redact_payload({"input_reference": encoded})["input_reference"]

    assert masked.startswith("iVBORw0KGgo")
    assert "***" in masked
    assert encoded[:32] in masked
    # Nothing beyond the prefix may leak.
    assert encoded[32:64] not in masked
    assert "image/png" in masked
    assert f"{len(encoded)} chars" in masked


def test_data_uri_keeps_its_header_and_prefix() -> None:
    encoded = base64.b64encode(_png()).decode()

    masked = redact_payload(
        {"input_reference": f"data:image/png;base64,{encoded}"}
    )["input_reference"]

    assert masked.startswith("data:image/png;base64,iVBORw0KGgo")
    assert "***" in masked
    assert encoded[32:64] not in masked


def test_nested_base64_is_masked_too() -> None:
    """`{"image_url": {"url": ...}}` and `b64_json` appear in real payloads."""
    encoded = base64.b64encode(_png()).decode()

    masked = redact_payload(
        {
            "images": [{"b64_json": encoded}, {"image_url": {"url": encoded}}],
            "reference_images": [encoded],
        }
    )

    for blob in (
        masked["images"][0]["b64_json"],
        masked["images"][1]["image_url"]["url"],
        masked["reference_images"][0],
    ):
        assert "***" in blob
        assert encoded[32:64] not in blob


def test_ordinary_fields_are_left_readable() -> None:
    payload = {
        "prompt": "让这只猫跑起来",
        "model": "doubao/seedance-2.0-mini",
        "seconds": "8",
        "size": "1280x720",
    }

    assert redact_payload(payload) == payload


def test_a_long_text_prompt_is_truncated_not_masked() -> None:
    """A verbose prompt must stay readable; only encoded media is masked."""
    prompt = "一只猫" * 1000

    masked = redact_payload({"prompt": prompt})["prompt"]

    assert masked.startswith("一只猫")
    assert masked.endswith(f"({len(prompt)} chars total)")


def test_short_or_non_media_base64_is_not_treated_as_media() -> None:
    """A base64-looking id must not be reported as an asset."""
    tiny = base64.b64encode(b"hello world").decode()

    assert redact_payload({"request_id": tiny})["request_id"] == tiny


def test_payload_logging_can_be_disabled(monkeypatch) -> None:
    from opentoken.api import payload_logging

    monkeypatch.setenv("OPENTOKEN_LOG_PAYLOADS", "0")
    assert payload_logging.payload_logging_enabled() is False
    monkeypatch.setenv("OPENTOKEN_LOG_PAYLOADS", "1")
    assert payload_logging.payload_logging_enabled() is True


def test_the_gateway_logs_request_and_response_bodies(caplog) -> None:
    """Bodies must be visible in the log, and the request must still reach the route.

    The middleware wraps `receive` instead of reading the body, so a regression
    here would show up as a 422 (empty body) rather than a logging failure.
    """
    client = TestClient(create_app())

    with caplog.at_level(logging.INFO, logger="opentoken.api.payload"):
        response = client.post(
            "/v1/videos",
            json={"prompt": "一只小猫", "audio": "https://cdn.example/bgm.mp3"},
        )

    # The body reached the route: audio is rejected by the handler, not by a 422.
    assert response.status_code == 400
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "api_request method=POST path=/v1/videos" in messages
    assert '"prompt": "一只小猫"' in messages
    assert "api_response method=POST path=/v1/videos status=400" in messages
    # The masked payload still shows the field name.
    assert "audio" in messages


@pytest.mark.parametrize("field", ["input_reference", "image", "reference_images"])
def test_a_base64_reference_never_appears_verbatim_in_the_log(caplog, field) -> None:
    client = TestClient(create_app())
    encoded = base64.b64encode(_png()).decode()

    with caplog.at_level(logging.INFO, logger="opentoken.api.payload"):
        client.post(
            "/v1/videos",
            json={"prompt": "一只猫", field: encoded},
        )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert encoded not in messages
    assert encoded[32:64] not in messages
