"""OpenAI-compatible ``/v1/images/generations``.

The only backend today is Doubao's Seedream web skill, which is driven through a
logged-in browser session (see :mod:`opentoken.providers.doubao_media`). The
response shape is OpenAI's: ``{"created": …, "data": [{"url"|"b64_json": …}]}``.

Doubao returns signed CDN links whose ``x-expires`` runs to 2036, so the default
``response_format=url`` hands back a URL that stays usable — no gateway-side
byte cache, and therefore no unauthenticated file route to secure. Callers that
need the pixels inline can ask for ``b64_json`` instead; we fetch the CDN object
and base64 it here.
"""
from __future__ import annotations

import base64
from time import time
from typing import Any

import httpx
from fastapi import APIRouter

from opentoken.api.errors import classify_provider_runtime_error, openai_error_response
from opentoken.api.reference_assets import (
    ReferenceAssetError,
    audio_unsupported_message,
    is_audio_field,
    references_from_payload,
)
from opentoken.gateway.router import get_default_router
from opentoken.models.media import DEFAULT_IMAGE_MODEL, IMAGE, list_media_models, resolve_media_model
from opentoken.providers.base import ProviderRateLimitError

router = APIRouter()

# Each image is a separate browser turn (~20-40s); a larger n just serialises
# more turns behind one request, so cap it instead of letting a client ask for
# 100 renders that will all fight over the same session lock.
_MAX_IMAGES_PER_REQUEST = 4
_DEFAULT_TIMEOUT_SECONDS = 240.0
_MAX_TIMEOUT_SECONDS = 600.0
_DOWNLOAD_TIMEOUT_SECONDS = 120.0


@router.post("/v1/images/generations")
def images_generations(payload: dict[str, Any]):
    if not isinstance(payload, dict):
        return _bad_request("Request body must be a JSON object.")

    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return _bad_request("`prompt` is required.", param="prompt")

    # The image skill is text-to-image only, so an audio field is refused by name
    # rather than ignored (see `reference_assets.references_from_payload`).
    for key in payload:
        if is_audio_field(key) and payload.get(key) not in (None, "", [], {}):
            return _bad_request(audio_unsupported_message(key), param=key)
    if references_from_payload(payload):
        return _bad_request(
            "Reference images are not supported by /v1/images/generations: "
            "describe the image in `prompt`. (Image-to-image is not exposed by "
            "Doubao's web image skill through this gateway.)",
            param="image",
        )

    model_ref = str(payload.get("model") or DEFAULT_IMAGE_MODEL).strip()
    resolved = resolve_media_model(model_ref, kind=IMAGE)
    if resolved is None:
        return _bad_request(
            f"Unknown image model: {model_ref}. Available: "
            + ", ".join(model.id for model in list_media_models(kind=IMAGE)),
            param="model",
        )

    count = _positive_int(payload.get("n"), default=1)
    if count is None:
        return _bad_request("`n` must be a positive integer.", param="n")
    count = min(count, _MAX_IMAGES_PER_REQUEST)

    response_format = str(payload.get("response_format") or "url").strip().lower()
    if response_format not in ("url", "b64_json"):
        return _bad_request(
            "`response_format` must be `url` or `b64_json`.", param="response_format"
        )

    requested_timeout = payload.get("timeout")
    timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
    if requested_timeout is not None:
        parsed_timeout = _positive_float(requested_timeout)
        if parsed_timeout is None:
            return _bad_request("`timeout` must be a positive number of seconds.", param="timeout")
        timeout_seconds = min(parsed_timeout, _MAX_TIMEOUT_SECONDS)

    try:
        assets = get_default_router().generate_images(
            model=resolved.id,
            prompt=prompt,
            count=count,
            timeout_seconds=timeout_seconds,
        )
    except ProviderRateLimitError as exc:
        return openai_error_response(
            status_code=429,
            message=str(exc),
            error_type="rate_limit_error",
        )
    except RuntimeError as exc:
        status_code, error_type = classify_provider_runtime_error(exc)
        return openai_error_response(
            status_code=_status_for_timeout(status_code, exc),
            message=str(exc),
            error_type=error_type,
        )
    except httpx.HTTPError as exc:
        return openai_error_response(
            status_code=502,
            message=f"Upstream provider error ({type(exc).__name__}).",
            error_type="api_error",
        )

    data: list[dict[str, Any]] = []
    for asset in assets[:count]:
        if response_format == "b64_json":
            try:
                encoded = _download_base64(asset.url)
            except httpx.HTTPError as exc:
                return openai_error_response(
                    status_code=502,
                    message=f"Failed to fetch the generated image ({type(exc).__name__}).",
                    error_type="api_error",
                )
            item: dict[str, Any] = {"b64_json": encoded}
        else:
            item = {"url": asset.url}
        if asset.revised_prompt:
            item["revised_prompt"] = asset.revised_prompt
        data.append(item)

    if not data:
        return openai_error_response(
            status_code=502,
            message="The provider returned no generated images.",
            error_type="api_error",
        )

    return {
        "created": int(time()),
        "model": resolved.id,
        "data": data,
    }


def _download_base64(url: str) -> str:
    response = httpx.get(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True)
    response.raise_for_status()
    return base64.b64encode(response.content).decode("ascii")


def _status_for_timeout(status_code: int, exc: Exception) -> int:
    if "timed out" in str(exc).lower():
        return 504
    return status_code


def _positive_int(value: Any, *, default: int) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _bad_request(message: str, *, param: str | None = None):
    return openai_error_response(
        status_code=400,
        message=message,
        error_type="invalid_request_error",
        param=param,
    )
