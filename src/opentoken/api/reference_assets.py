"""Turn a caller's reference assets into local files Doubao can be fed.

The OpenAI SDK sends `input_reference` as a multipart file part, but curl/JSON
callers reasonably pass a URL or a data URI instead, so both encodings are
accepted. Doubao's web video skill then needs an actual file on disk: Playwright
uploads by path, not by URL.

**Images are supported, audio is not.** The skill's file input is
`accept=".jpg,.png,.jpeg,.webp,.apng"`, and when an audio file is forced through
it anyway the UI builds an `<img>` card (`data-kind="image"`, `alt="x.wav"`),
i.e. the audio would be handed to the model as a broken picture. Audio is
therefore rejected with an explanation instead of being quietly mangled.
"""
from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from opentoken.models.media import (
    MAX_REFERENCE_IMAGES,
    MAX_REFERENCE_IMAGE_BYTES,
)

_ALLOWED_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".apng")
_SUFFIX_BY_MEDIA_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/apng": ".apng",
}
_MEDIA_TYPE_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".apng": "image/apng",
}
# Magic numbers, so a JPEG renamed to .png is still recognised (and so audio is
# caught even when the upload has no useful filename).
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"RIFF", "riff"),  # WebP, WAV and AVI all start with RIFF; disambiguated below
)
_AUDIO_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"RIFF", "audio/wav"),
    (b"ID3", "audio/mpeg"),
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
    (b"#!AMR", "audio/amr"),
)

_REFERENCE_KEYS = ("input_reference", "image", "images", "reference_images")
# File parts a create request may legitimately carry; anything else is a client
# mistake (or audio, which has its own explanation).
_FILE_FIELD_KEYS = _REFERENCE_KEYS
# Audio is not merely unsupported by the upload input: Doubao's web video skill
# has no audio track input at all. Keys that name an audio asset are rejected by
# name so a caller never believes their soundtrack was used.
_UNSUPPORTED_MEDIA_KEYS = (
    "audio",
    "audios",
    "audio_url",
    "audio_file",
    "music",
    "soundtrack",
    "bgm",
)
_AUDIO_SUFFIXES = (
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".amr",
    ".wma",
    ".aiff",
)



class ReferenceAssetError(ValueError):
    """A caller-provided reference cannot be used; the message is user-facing."""

    def __init__(self, message: str, *, param: str = "input_reference") -> None:
        super().__init__(message)
        self.param = param


@dataclass(frozen=True)
class ReferenceUpload:
    """One file part from a multipart request."""

    filename: str
    content: bytes


def is_audio_field(name: str) -> bool:
    """Whether a request field names an audio asset (and so must be refused)."""
    return str(name or "").strip().lower() in _UNSUPPORTED_MEDIA_KEYS


def is_reference_file_field(name: str) -> bool:
    """Whether a multipart file part under `name` is a reference image."""
    return str(name or "").strip().lower() in _REFERENCE_KEYS


def references_from_payload(payload: dict) -> list[object]:
    """Collect the raw reference specs a JSON payload carries, in caller order.

    Audio fields raise instead of being skipped: the web video skill cannot use
    them, and silently dropping an `audio` key would let a caller believe their
    soundtrack was applied.
    """
    for key in _UNSUPPORTED_MEDIA_KEYS:
        value = payload.get(key)
        if value in (None, "", [], {}):
            continue
        raise ReferenceAssetError(_AUDIO_UNSUPPORTED_MESSAGE(key, "audio"), param=key)
    specs: list[object] = []
    for key in _REFERENCE_KEYS:
        value = payload.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, (list, tuple)):
            specs.extend(value)
        else:
            specs.append(value)
    return specs


def _sniff(data: bytes) -> str | None:
    """Best-effort media type from the leading bytes."""
    if data[:4] == b"RIFF" and len(data) >= 12:
        form = data[8:12]
        if form == b"WEBP":
            return "image/webp"
        if form == b"WAVE":
            return "audio/wav"
        if form == b"AVI ":
            return "video/x-msvideo"
        return "riff"
    for magic, media_type in _IMAGE_MAGIC:
        if magic != b"RIFF" and data.startswith(magic):
            return media_type
    for magic, media_type in _AUDIO_MAGIC:
        if magic != b"RIFF" and data.startswith(magic):
            return media_type
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return "audio/mp4"
        return "video/mp4"
    return None


def detect_audio(data: bytes, *, filename: str = "") -> str | None:
    """Return a media type if these bytes are audio, else None.

    Doubao's web video skill has no audio input at all, so anything musical has
    to be refused up front; a caller deserves a specific error rather than a
    render that silently ignores their soundtrack.
    """
    media_type = _sniff(data)
    if media_type and media_type.startswith("audio/"):
        return media_type
    lowered = (filename or "").lower()
    if lowered.endswith(_AUDIO_SUFFIXES):
        return "audio/unknown"
    return None


def detect_unsupported_media(data: bytes, *, filename: str = "") -> str | None:
    """Return a media type if these bytes are audio or video, else None.

    Video is refused for the same reason as audio (the input is image-only); it
    is reported separately so the error can say which one the caller sent.
    """
    audio = detect_audio(data, filename=filename)
    if audio:
        return audio
    media_type = _sniff(data)
    if media_type and media_type.startswith("video/"):
        return media_type
    lowered = (filename or "").lower()
    if lowered.endswith((".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v")):
        return "video/unknown"
    return None


def _suffix_for(data: bytes, *, filename: str, media_type: str | None) -> str:
    lowered = (filename or "").lower()
    for suffix in _ALLOWED_IMAGE_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    if media_type in _SUFFIX_BY_MEDIA_TYPE:
        return _SUFFIX_BY_MEDIA_TYPE[media_type]
    sniffed = _sniff(data)
    if sniffed in _SUFFIX_BY_MEDIA_TYPE:
        return _SUFFIX_BY_MEDIA_TYPE[sniffed]
    return ".png"


def _check_image(data: bytes, *, filename: str, media_type: str | None) -> str:
    if len(data) > MAX_REFERENCE_IMAGE_BYTES:
        raise ReferenceAssetError(
            f"Reference image '{filename or '<unnamed>'}' is larger than "
            f"{MAX_REFERENCE_IMAGE_BYTES // (1024 * 1024)}MB."
        )
    if not data:
        raise ReferenceAssetError("Reference image is empty.")
    unsupported = detect_unsupported_media(data, filename=filename)
    if unsupported:
        if unsupported.startswith("audio/"):
            raise ReferenceAssetError(_AUDIO_UNSUPPORTED_MESSAGE(filename, unsupported))
        raise ReferenceAssetError(_VIDEO_UNSUPPORTED_MESSAGE(filename, unsupported))
    if not _looks_like_image(data, filename, media_type):
        raise ReferenceAssetError(
            f"Reference image '{filename or '<unnamed>'}' does not look like a "
            "supported image (jpg, png, webp, apng)."
        )
    return _suffix_for(data, filename=filename, media_type=media_type)


def _looks_like_image(data: bytes, filename: str, media_type: str | None) -> bool:
    if media_type in _SUFFIX_BY_MEDIA_TYPE:
        return True
    lowered = (filename or "").lower()
    if lowered.endswith(_ALLOWED_IMAGE_SUFFIXES):
        return True
    sniffed = _sniff(data)
    return bool(sniffed) and (
        sniffed.startswith("image/") or sniffed == "riff"
    )


def audio_unsupported_message(label: str, media_type: str | None = None) -> str:
    """The one explanation for refusing audio, reused by every entry point."""
    return _AUDIO_UNSUPPORTED_MESSAGE(label, media_type or "audio")


def _AUDIO_UNSUPPORTED_MESSAGE(filename: str, media_type: str) -> str:
    return (
        f"Audio is not supported ('{filename or media_type}'): Doubao's web video "
        "skill generates silent video and has no audio input — its upload input "
        "takes images only (jpg/png/webp/apng). An audio file forced through it is "
        "attached as a broken image, so it is rejected instead of being ignored. "
        "Describe the soundscape in `prompt`, or add audio after the render."
    )


def _VIDEO_UNSUPPORTED_MESSAGE(filename: str, media_type: str) -> str:
    return (
        f"Video input is not supported ('{filename or media_type}'): the video "
        "skill's upload input takes images only (jpg/png/webp/apng), and it "
        "already produces video. Use a still reference image, or describe the "
        "motion in `prompt`."
    )


def _decode_data_uri(spec: str) -> tuple[bytes, str | None]:
    header, _, payload = spec.partition(",")
    if not payload:
        raise ReferenceAssetError("Malformed data URI in `input_reference`.")
    media_type = None
    match = re.match(r"^data:([^;]+)", header, re.IGNORECASE)
    if match:
        media_type = match.group(1).strip().lower()
    try:
        if ";base64" in header.lower():
            return base64.b64decode(payload, validate=True), media_type
        from urllib.parse import unquote_to_bytes

        return unquote_to_bytes(payload), media_type
    except (binascii.Error, ValueError) as exc:
        raise ReferenceAssetError("Malformed base64 in `input_reference`.") from exc


def _download(spec: str) -> tuple[bytes, str | None]:
    try:
        response = httpx.get(
            spec,
            follow_redirects=True,
            timeout=httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0),
        )
    except httpx.HTTPError as exc:
        raise ReferenceAssetError(
            f"Could not download `input_reference` ({type(exc).__name__})."
        ) from exc
    if response.status_code >= 400:
        raise ReferenceAssetError(
            f"Could not download `input_reference` (upstream returned {response.status_code})."
        )
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return response.content, content_type or None


_BASE64_CHARS = re.compile(r"^[A-Za-z0-9+/=_\-\s]+$")


def _try_base64(text: str) -> bytes | None:
    """Decode a bare base64 payload (no ``data:`` prefix), or return None.

    Clients routinely pass raw base64 — e.g. a `b64_json` value copied straight
    from another response — so requiring the `data:` prefix would reject them.
    Decoding alone is not proof, though: a short slug or a local path can also
    be valid base64. The bytes therefore have to be recognisably an image, audio
    or video before they are accepted, which keeps a typo from surfacing as a
    confusing "not a supported image" error.
    """
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 32 or len(compact) % 4 != 0:
        return None
    if not _BASE64_CHARS.match(compact):
        return None
    # Accept the URL-safe alphabet too; both appear in the wild.
    normalised = compact.replace("-", "+").replace("_", "/")
    try:
        data = base64.b64decode(normalised, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data:
        return None
    if _sniff(data) is None:
        return None
    return data


def _spec_to_bytes(spec: object) -> tuple[bytes, str, str | None]:
    """Resolve one JSON reference spec into `(bytes, filename hint, media type)`."""
    if isinstance(spec, dict):
        nested = None
        for key in ("image_url", "url", "data", "b64_json", "content"):
            if key in spec:
                nested = spec[key]
                break
        if nested is None:
            raise ReferenceAssetError(
                "Unsupported `input_reference` object; expected `url` or `image_url`."
            )
        # `{"image_url": {"url": "..."}}` is how chat-style payloads nest it.
        if isinstance(nested, dict):
            if "url" not in nested:
                raise ReferenceAssetError(
                    "Unsupported `input_reference` object; expected `url` or `image_url`."
                )
            nested = nested["url"]
        return _spec_to_bytes(nested)
    if isinstance(spec, (bytes, bytearray)):
        return bytes(spec), "", None

    text = str(spec).strip()
    if not text:
        raise ReferenceAssetError("`input_reference` is empty.")
    if text.startswith("data:"):
        data, media_type = _decode_data_uri(text)
        return data, "", media_type
    if re.match(r"^https?://", text, re.IGNORECASE):
        data, media_type = _download(text)
        return data, text.rsplit("/", 1)[-1][:120], media_type
    # A bare path is not something a remote caller can use, but a local one is
    # handy for curl-based debugging.
    candidate = Path(text)
    if candidate.is_file():
        return candidate.read_bytes(), candidate.name, None
    # A bare base64 body (no `data:` prefix) is the common client encoding.
    decoded = _try_base64(text)
    if decoded is not None:
        return decoded, "", _sniff(decoded)
    raise ReferenceAssetError(
        "`input_reference` must be an uploaded file, an http(s) URL, a data URI, "
        "or a base64-encoded image."
    )


def materialize_references(
    specs: list[object],
    uploads: list[ReferenceUpload],
    *,
    workdir: Path,
) -> list[Path]:
    """Write every reference image into `workdir` and return the file paths.

    Raises :class:`ReferenceAssetError` for anything unusable (wrong type, too
    large, unsupported audio) so the route can answer 400 with the reason.
    """
    resolved: list[tuple[bytes, str, str | None]] = [
        (upload.content, upload.filename, None) for upload in uploads
    ]
    for spec in specs:
        resolved.extend([_spec_to_bytes(spec)])

    if len(resolved) > MAX_REFERENCE_IMAGES:
        raise ReferenceAssetError(
            f"At most {MAX_REFERENCE_IMAGES} reference images are supported "
            f"({len(resolved)} were supplied)."
        )
    if not resolved:
        return []

    workdir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, (data, filename, media_type) in enumerate(resolved, start=1):
        suffix = _check_image(data, filename=filename, media_type=media_type)
        target = workdir / f"reference-{index}{suffix}"
        target.write_bytes(data)
        paths.append(target)
    return paths


def describe_reference_media_types(paths: list[Path]) -> list[str]:
    """Media types of the materialized files (used in logs only)."""
    out: list[str] = []
    for path in paths:
        out.append(_MEDIA_TYPE_BY_SUFFIX.get(path.suffix.lower(), "image/png"))
    return out
