"""`capability` is part of the /v1/models contract, so it is tested in isolation."""
from __future__ import annotations

import pytest

from opentoken.models.capabilities import (
    AUDIO,
    CAPABILITIES,
    DEFAULT_CAPABILITY,
    IMAGE,
    TEXT,
    VIDEO,
    normalize_capability,
)
from opentoken.models.catalog import ModelCatalogEntry
from opentoken.models.media import MEDIA_MODELS, resolve_media_model
from opentoken.models.openai_compat import build_openai_model_objects


def test_every_advertised_model_declares_exactly_one_known_capability() -> None:
    """A client picks the endpoint from this field — an unknown value is useless."""
    assert CAPABILITIES == (TEXT, IMAGE, VIDEO, AUDIO)
    assert DEFAULT_CAPABILITY == TEXT


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("image", IMAGE),
        ("  IMAGE  ", IMAGE),
        ("video", VIDEO),
        ("audio", AUDIO),
        ("text", TEXT),
        (None, TEXT),
        ("", TEXT),
        ("embeddings", TEXT),  # unknown values must not leak into the wire format
    ],
)
def test_normalize_capability_keeps_the_enum_closed(raw, expected) -> None:
    assert normalize_capability(raw) == expected


def test_chat_catalog_entries_default_to_text() -> None:
    entry = ModelCatalogEntry(id="algae/doubao/doubao-pro", provider="opentoken", name="Doubao 专家")

    assert entry.capability == TEXT
    assert entry.to_openai_dict()["capability"] == TEXT


def test_build_openai_model_objects_carries_capability() -> None:
    entries = [
        ModelCatalogEntry(id="algae/qwen-intl/qwen3.6-plus", provider="opentoken", name="Qwen3.6-Plus")
    ]

    assert build_openai_model_objects(entries) == [
        {
            "id": "qwen-intl/qwen3.6-plus",
            "object": "model",
            "owned_by": "opentoken",
            "capability": TEXT,
        }
    ]


def test_media_models_advertise_their_own_kind() -> None:
    """Seedream must never look like a chat model in the catalog."""
    for model in MEDIA_MODELS:
        assert model.to_openai_dict()["capability"] == model.kind

    seedream = resolve_media_model("doubao/seedream-5.0-flash", kind="image")
    seedance = resolve_media_model("doubao/seedance-2.0-mini", kind="video")
    assert seedream is not None and seedream.to_openai_dict()["capability"] == IMAGE
    assert seedance is not None and seedance.to_openai_dict()["capability"] == VIDEO
