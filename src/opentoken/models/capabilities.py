"""What a model produces, advertised as `capability` in `/v1/models`.

A bare ``id`` like ``doubao/seedance-2.0-mini`` doesn't tell a client whether to
send it to ``/v1/chat/completions`` or ``/v1/videos``, so every entry carries one
capability value:

- ``text``  → ``/v1/chat/completions``
- ``image`` → ``/v1/images/generations``
- ``video`` → ``/v1/videos``
- ``audio`` → reserved; the gateway has no audio model yet

The capability describes the model's *output*. Chat models always produce text,
even when they accept image input, because the input side is a per-request
feature rather than a property of the model id.
"""
from __future__ import annotations

TEXT = "text"
IMAGE = "image"
VIDEO = "video"
AUDIO = "audio"

CAPABILITIES: tuple[str, ...] = (TEXT, IMAGE, VIDEO, AUDIO)

#: What chat/discovery models produce — they are all text today.
DEFAULT_CAPABILITY = TEXT


def normalize_capability(value: str | None) -> str:
    """Coerce an arbitrary string into a known capability.

    Unknown values fall back to ``text`` instead of leaking a typo into the
    wire format; ``/v1/models`` is a contract, so the enum has to hold.
    """
    lowered = str(value or "").strip().lower()
    return lowered if lowered in CAPABILITIES else DEFAULT_CAPABILITY


__all__ = [
    "AUDIO",
    "CAPABILITIES",
    "DEFAULT_CAPABILITY",
    "IMAGE",
    "TEXT",
    "VIDEO",
    "normalize_capability",
]
