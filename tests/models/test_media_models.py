from pathlib import Path

import pytest

from opentoken.config.paths import resolve_providers_dir
from opentoken.models.media import (
    DEFAULT_IMAGE_MODEL,
    DEFAULT_VIDEO_MODEL,
    IMAGE,
    MEDIA_MODELS,
    VIDEO,
    list_media_models,
    media_models_for_logged_in_providers,
    resolve_media_model,
)
from opentoken.models.provider_credentials import ProviderCredentialRecord
from opentoken.storage.provider_store import save_provider_credentials


def test_resolve_accepts_prefixed_bare_and_legacy_ids() -> None:
    for ref in (
        "doubao/seedream-5.0-flash",
        "seedream-5.0-flash",
        "algae/doubao/seedream-5.0-flash",
        "DOUBAO/Seedream-5.0-Flash",
    ):
        resolved = resolve_media_model(ref, kind=IMAGE)
        assert resolved is not None, ref
        assert resolved.id == "doubao/seedream-5.0-flash"
        assert resolved.label == "Seedream 5.0 Flash"


def test_resolve_accepts_aliases() -> None:
    assert resolve_media_model("seedream", kind=IMAGE).id == "doubao/seedream-4.0"  # type: ignore[union-attr]
    assert resolve_media_model("seedance", kind=VIDEO).id == "doubao/seedance-2.0"  # type: ignore[union-attr]
    assert resolve_media_model("seedance-mini", kind=VIDEO).id == "doubao/seedance-2.0-mini"  # type: ignore[union-attr]


def test_resolve_rejects_wrong_kind_unknown_and_other_providers() -> None:
    assert resolve_media_model("doubao/seedream-5.0-flash", kind=VIDEO) is None
    assert resolve_media_model("doubao/seedance-2.0", kind=IMAGE) is None
    assert resolve_media_model("doubao/gpt-image-1", kind=IMAGE) is None
    assert resolve_media_model("openai/dall-e-3", kind=IMAGE) is None
    assert resolve_media_model("qwen/seedream-5.0-flash", kind=IMAGE) is None
    assert resolve_media_model("", kind=IMAGE) is None


def test_defaults_are_listed_media_models() -> None:
    image_ids = {model.id for model in list_media_models(kind=IMAGE)}
    video_ids = {model.id for model in list_media_models(kind=VIDEO)}
    assert DEFAULT_IMAGE_MODEL in image_ids
    assert DEFAULT_VIDEO_MODEL in video_ids
    assert image_ids | video_ids == {model.id for model in MEDIA_MODELS}
    assert image_ids.isdisjoint(video_ids)


def test_every_media_model_uses_the_bare_provider_prefix() -> None:
    for model in MEDIA_MODELS:
        assert "/" in model.id, model.id
        assert model.id.startswith(f"{model.provider}/"), model.id
        assert not model.id.startswith("algae/"), model.id
        assert model.provider_model == model.id.split("/", 1)[1]


def test_logged_in_filter_requires_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENTOKEN_STATE_DIR", str(tmp_path))
    assert media_models_for_logged_in_providers() == []

    save_provider_credentials(
        resolve_providers_dir(),
        ProviderCredentialRecord(
            provider="doubao", kind="cookie", cookie="sessionid=abc", status="valid"
        ),
    )

    listed = media_models_for_logged_in_providers()
    assert {model.id for model in listed} == {model.id for model in MEDIA_MODELS}
    assert media_models_for_logged_in_providers(kind=VIDEO) == list_media_models(kind=VIDEO)
