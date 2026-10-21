"""Image / video generation model registry.

These live outside :mod:`opentoken.models.discovery` on purpose: discovery
enumerates each provider's *chat* lineup, while Doubao's media models come from
a different UI menu (图像生成 → Seedream, 视频生成 → Seedance) and never show up
in the chat `/v1/models` payload. Keeping them here also means a logged-out
gateway never advertises media capabilities it cannot serve — callers can ask
:func:`list_media_models` only for providers that actually have credentials.

Wire ids follow the same bare ``<provider>/<model>`` convention as chat, so a
client that already discovered ``doubao/doubao-pro`` can request
``doubao/seedream-5.0-flash`` without learning a second naming scheme.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from opentoken.config.paths import resolve_providers_dir
from opentoken.models.capabilities import IMAGE, VIDEO, normalize_capability
from opentoken.storage.provider_store import load_provider_credentials

# Media models keep declaring `kind` (routes branch on image vs video); it is the
# same value as the advertised capability, so the two can't drift.


@dataclass(frozen=True)
class MediaModel:
    id: str
    provider: str
    kind: str
    label: str
    aliases: tuple[str, ...] = ()
    # Whether Doubao's web skill lets this model take a reference image. The UI
    # says so itself: selecting Seedance 2.0 Fast shows "视觉理解能力较弱，暂不支持
    # 参考图输入。如有需要请切换至 Seedance 2.0 或 Seedance 2.0 Mini".
    supports_reference_image: bool = True

    @property
    def provider_model(self) -> str:
        return self.id.split("/", 1)[1]

    def to_openai_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "object": "model",
            "owned_by": "opentoken",
            "capability": normalize_capability(self.kind),
        }


# The ``label`` strings must match the options Doubao renders in its media model
# menu (`模型 Seedream 5.0 Flash`), because the browser flow selects a model by
# clicking that exact row.
MEDIA_MODELS: tuple[MediaModel, ...] = (
    MediaModel(
        id="doubao/seedream-5.0-flash",
        provider="doubao",
        kind=IMAGE,
        label="Seedream 5.0 Flash",
        aliases=("seedream-5.0", "seedream-5", "seedream-5-flash", "doubao-seedream-5.0-flash"),
    ),
    MediaModel(
        id="doubao/seedream-5.0-pro",
        provider="doubao",
        kind=IMAGE,
        label="Seedream 5.0 Pro",
        aliases=("seedream-5-pro", "seedream-5.0-flash-pro"),
    ),
    MediaModel(
        id="doubao/seedream-4.5",
        provider="doubao",
        kind=IMAGE,
        label="Seedream 4.5",
        aliases=("seedream-4-5", "seedream-45"),
    ),
    MediaModel(
        id="doubao/seedream-4.0",
        provider="doubao",
        kind=IMAGE,
        label="Seedream 4.0",
        aliases=("seedream-4-0", "seedream-40", "seedream"),
    ),
    MediaModel(
        id="doubao/seedance-2.5",
        provider="doubao",
        kind=VIDEO,
        label="Seedance 2.5",
    ),
    MediaModel(
        id="doubao/seedance-2.0",
        provider="doubao",
        kind=VIDEO,
        label="Seedance 2.0",
        aliases=("seedance",),
    ),
    MediaModel(
        id="doubao/seedance-2.0-fast",
        provider="doubao",
        kind=VIDEO,
        label="Seedance 2.0 Fast",
        aliases=("seedance-fast",),
        supports_reference_image=False,
    ),
    MediaModel(
        id="doubao/seedance-2.0-mini",
        provider="doubao",
        kind=VIDEO,
        label="Seedance 2.0 Mini",
        aliases=("seedance-mini",),
    ),
)

DEFAULT_IMAGE_MODEL = "doubao/seedream-5.0-flash"
DEFAULT_VIDEO_MODEL = "doubao/seedance-2.0-mini"

# Doubao's video skill exposes aspect ratio as a row of buttons and duration as a
# 4s–15s slider; these are upstream's limits, so the API validates against the
# same numbers instead of inventing its own. "自动" is the unset state.
VIDEO_RATIO_AUTO = "自动"
VIDEO_RATIOS: tuple[str, ...] = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
VIDEO_SECONDS_MIN = 4
VIDEO_SECONDS_MAX = 15
# Up to this many reference images; Doubao's file input takes multiple, but each
# one is another upload, and more than a few stops being a "reference".
MAX_REFERENCE_IMAGES = 4
MAX_REFERENCE_IMAGE_BYTES = 10 * 1024 * 1024


def video_ratio_for_size(size: str) -> str:
    """Map a ``WxH`` size onto the closest ratio Doubao can render.

    `size` is already validated as ``WxH`` by the route. Doubao only offers the
    ratios in :data:`VIDEO_RATIOS`, so an arbitrary size has to be projected onto
    one of them; comparison is done on the log of the aspect ratio, which treats
    portrait and landscape symmetrically (3:4 is as close to 1:1 as 4:3 is).
    """
    match = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", str(size or ""), re.IGNORECASE)
    if match is None:
        raise ValueError(f"Unparseable size: {size!r}")
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        raise ValueError(f"Unparseable size: {size!r}")
    target = math.log(width / height)

    def distance(ratio: str) -> float:
        ratio_width, ratio_height = ratio.split(":")
        return abs(math.log(int(ratio_width) / int(ratio_height)) - target)

    return min(VIDEO_RATIOS, key=distance)


def list_media_models(kind: str | None = None) -> list[MediaModel]:
    if kind is None:
        return list(MEDIA_MODELS)
    return [model for model in MEDIA_MODELS if model.kind == kind]


def resolve_media_model(model_ref: str, *, kind: str | None = None) -> MediaModel | None:
    """Resolve a client-supplied model reference to a media model.

    Accepts the advertised ``doubao/seedream-5.0-flash`` id, the bare
    ``seedream-5.0-flash`` form (an unprefixed media id can't collide with a
    chat model, so it is unambiguous), the legacy ``algae/doubao/…`` namespace,
    and each model's aliases. Matching is case-insensitive. Returns ``None`` for
    anything unknown or for a model of the wrong ``kind``.
    """
    cleaned = str(model_ref or "").strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if lowered.startswith("algae/"):
        lowered = lowered[len("algae/") :]
    parts = lowered.split("/")
    if len(parts) >= 3:
        return None
    requested = parts[-1]

    for model in MEDIA_MODELS:
        if kind is not None and model.kind != kind:
            continue
        provider_ok = len(parts) == 1 or parts[0] == model.provider
        if not provider_ok:
            continue
        if requested == model.provider_model.lower():
            return model
        if requested in {alias.lower() for alias in model.aliases}:
            return model
    return None


def media_models_for_logged_in_providers(
    *,
    kind: str | None = None,
    providers_dir: Path | None = None,
) -> list[MediaModel]:
    """Media models whose provider has stored credentials.

    /v1/models uses this so it never invites a caller to hit a provider it isn't
    logged in to — the same rule discovery applies to chat models.
    """
    resolved_dir = providers_dir or resolve_providers_dir()
    availability: dict[str, bool] = {}
    available: list[MediaModel] = []
    for model in MEDIA_MODELS:
        if kind is not None and model.kind != kind:
            continue
        if model.provider not in availability:
            availability[model.provider] = (
                load_provider_credentials(resolved_dir, model.provider) is not None
            )
        if availability[model.provider]:
            available.append(model)
    return available


__all__ = [
    "DEFAULT_IMAGE_MODEL",
    "DEFAULT_VIDEO_MODEL",
    "IMAGE",
    "MAX_REFERENCE_IMAGES",
    "MAX_REFERENCE_IMAGE_BYTES",
    "MEDIA_MODELS",
    "VIDEO",
    "VIDEO_RATIO_AUTO",
    "VIDEO_RATIOS",
    "VIDEO_SECONDS_MAX",
    "VIDEO_SECONDS_MIN",
    "MediaModel",
    "list_media_models",
    "media_models_for_logged_in_providers",
    "resolve_media_model",
    "video_ratio_for_size",
]
