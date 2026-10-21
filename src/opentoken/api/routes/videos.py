"""Doubao Seedance video generation, in two wire shapes.

Doubao renders video asynchronously (it answers "预计等待 5 分钟" and pushes the
clip as a later chat message), so both shapes are job-based.

**OpenAI Sora shape** — what the official SDK speaks:

    POST   /v1/videos                    -> Video {id, status, progress, ...}
    GET    /v1/videos/{id}               -> Video
    GET    /v1/videos/{id}/content       -> the MP4 bytes
    GET    /v1/videos                    -> {"object": "list", "data": [...]}
    DELETE /v1/videos/{id}

`GET /v1/videos` previously answered ``404 {"detail":"Not Found"}`` — FastAPI's
default for an unregistered path — which is what clients hitting
``client.videos.create(...)`` saw.

**`/generations` shape** — the first shape this gateway shipped, still supported:

    POST /v1/videos/generations          -> {"id", "status": "in_progress"}
    GET  /v1/videos/generations/{id}     -> {"status": "completed", "url"}

Pass ``"wait": true`` to either POST to block until the render finishes and get
the finished payload straight back. Jobs are kept in memory, but every media
task also writes `~/.opentoken/tasks/<taskId>.json`; a poll for an id this
process doesn't know is answered from that dump, so a gateway restart no longer
turns a successfully submitted render into `404 video_not_found` (an in-flight
one is reported as failed with `code: job_interrupted`).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import shutil
import tempfile
import threading
from itertools import count
from pathlib import Path
from time import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from opentoken.api.errors import classify_provider_runtime_error, openai_error_response
from opentoken.api.reference_assets import (
    ReferenceAssetError,
    ReferenceUpload,
    audio_unsupported_message,
    describe_reference_media_types,
    is_audio_field,
    is_reference_file_field,
    materialize_references,
    references_from_payload,
)
from opentoken.gateway.router import get_default_router
from opentoken.models.media import (
    DEFAULT_VIDEO_MODEL,
    VIDEO,
    VIDEO_SECONDS_MAX,
    VIDEO_SECONDS_MIN,
    list_media_models,
    resolve_media_model,
)
from opentoken.providers.base import ProviderRateLimitError
from opentoken.tasks import (
    TaskRecorder,
    load_task,
    mark_task_deleted,
    new_task_id,
    start_task,
)

logger = logging.getLogger("opentoken.api.videos")

router = APIRouter()

_DEFAULT_TIMEOUT_SECONDS = 600.0
_MAX_TIMEOUT_SECONDS = 900.0
_JOB_TTL_SECONDS = 60 * 60
_JOB_PRUNE_THRESHOLD = 64
# Doubao's own copy says "预计等待 5 分钟"; used only to turn elapsed time into
# the 0-99 `progress` number the OpenAI Video object carries. It is an estimate,
# never read back from upstream.
_EXPECTED_RENDER_SECONDS = 300.0
_DOWNLOAD_CONNECT_TIMEOUT_SECONDS = 30.0
_DOWNLOAD_READ_TIMEOUT_SECONDS = 300.0


_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
# Creation order for `GET /v1/videos`. `created_at` is whole seconds, so two
# renders submitted in the same second would otherwise sort unpredictably.
_JOB_SEQUENCE = count(1)


@router.post("/v1/videos/generations")
def videos_generations(payload: dict[str, Any], request: Request):
    """Legacy shape. Accepts the same `size` / `seconds` / `input_reference`."""
    if not isinstance(payload, dict):
        return _bad_request("Request body must be a JSON object.")
    task_id = new_task_id("vidgen")
    recorder = start_task(
        task_id,
        kind="video",
        request={
            "method": request.method,
            "path": request.url.path,
            "content_type": request.headers.get("content-type", ""),
            "format": "json",
            "body": payload,
        },
    )
    spec, rejection = _parse_video_request(payload, [])
    if rejection is not None:
        _finish_task(recorder, response=rejection, status="rejected")
        return rejection
    recorder = _record_spec(recorder, spec)

    wait = _as_bool(payload.get("wait"), default=False)
    if wait:
        outcome = _render_now(
            spec["model"],
            spec["prompt"],
            spec["timeout_seconds"],
            seconds=spec["seconds_value"],
            size=spec["size"],
            references=spec["references"],
            recorder=recorder,
        )
        if not isinstance(outcome, dict):
            _finish_task(recorder, response=outcome, status="failed")
            return outcome
        response = _completed_payload(spec["model"], outcome["asset"])
        _finish_task(
            recorder,
            response=response,
            status="completed",
            result={"asset": _asset_dump(outcome["asset"])},
        )
        return response

    job = _create_job(
        task_id,
        spec["model"],
        spec["prompt"],
        spec["timeout_seconds"],
        size=spec["size"],
        seconds=spec["seconds"],
        seconds_value=spec["seconds_value"],
        references=spec["references"],
        recorder=recorder,
    )
    response = _job_public_payload(job, request)
    _finish_task(recorder, response=response)
    return response


@router.get("/v1/videos/generations/{job_id}")
def videos_generations_retrieve(job_id: str, request: Request):
    snapshot = _job_snapshot(job_id)
    if snapshot is None:
        return _job_not_found(job_id)
    return _job_public_payload(snapshot, request)


# --------------------------------------------------------------------------- #
# OpenAI Sora shape: POST /v1/videos, GET /v1/videos/{id}[/content]
# --------------------------------------------------------------------------- #
@router.post("/v1/videos")
async def videos_create(request: Request):
    """Create a render, OpenAI-Sora style.

    The official SDK posts multipart when `input_reference` is present and JSON
    otherwise, so both encodings are accepted. A reference *image* becomes a real
    attachment in Doubao's composer (image-to-video); audio is rejected with 400,
    because the web skill's upload input takes images only and would attach an
    audio file as a broken picture. `size` and `seconds` are applied to Doubao's
    ratio buttons and duration slider instead of being echoed back.
    """
    task_id = new_task_id("vidgen")
    payload, uploads, rejection, observed = await _read_video_body(request)
    # Started before validation so a rejected request is still on disk: "the
    # reference never reached Doubao" is most often a 400 from this route, and
    # that body is the evidence.
    recorder = await run_in_threadpool(
        start_task,
        task_id,
        kind="video",
        request={
            "method": request.method,
            "path": request.url.path,
            "content_type": request.headers.get("content-type", ""),
            "content_length": request.headers.get("content-length"),
            **observed,
        },
    )
    if rejection is not None:
        await _finish_task_async(recorder, response=rejection, status="rejected")
        return rejection

    spec, rejection = _parse_video_request(payload, uploads)
    if rejection is not None:
        await _finish_task_async(recorder, response=rejection, status="rejected")
        return rejection
    recorder = await run_in_threadpool(_record_spec, recorder, spec)
    resolved = spec["resolved"]
    requested_size = spec["size"]
    requested_seconds = spec["seconds"]
    timeout_seconds = spec["timeout_seconds"]

    if _as_bool(payload.get("wait"), default=False):
        outcome = _render_now(
            resolved.id,
            spec["prompt"],
            timeout_seconds,
            seconds=spec["seconds_value"],
            size=requested_size,
            references=spec["references"],
            recorder=recorder,
        )
        if not isinstance(outcome, dict):
            await _finish_task_async(recorder, response=outcome, status="failed")
            return outcome
        # `wait` is an extension for curl/debugging: return the finished Video
        # object rather than the `/generations` payload, so an SDK-shaped caller
        # never gets a different envelope depending on this flag.
        job = _completed_job(
            task_id,
            resolved.id,
            spec["prompt"],
            timeout_seconds,
            outcome["asset"],
            size=requested_size,
            seconds=requested_seconds,
        )
        response = _openai_video_payload(job)
        await _finish_task_async(
            recorder,
            response=response,
            status="completed",
            result={"asset": _asset_dump(outcome["asset"])},
        )
        return response

    job = _create_job(
        task_id,
        resolved.id,
        spec["prompt"],
        timeout_seconds,
        size=requested_size,
        seconds=requested_seconds,
        seconds_value=spec["seconds_value"],
        references=spec["references"],
        recorder=recorder,
    )
    response = _openai_video_payload(job)
    await _finish_task_async(recorder, response=response)
    return response


def _parse_video_request(
    payload: dict[str, Any], uploads: list[ReferenceUpload]
) -> tuple[dict[str, Any], Any]:
    """Validate a create request, shared by both wire shapes.

    Returns `(spec, None)` on success or `({}, error_response)` on a bad request.
    Reference images are materialized here (into a temp dir) so the render worker
    only ever deals with local paths.
    """
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return {}, _bad_request("`prompt` is required.", param="prompt")

    model_ref = str(payload.get("model") or DEFAULT_VIDEO_MODEL).strip()
    resolved = resolve_media_model(model_ref, kind=VIDEO)
    if resolved is None:
        return {}, _bad_request(
            f"Unknown video model: {model_ref}. Available: "
            + ", ".join(model.id for model in list_media_models(kind=VIDEO)),
            param="model",
        )

    requested_size = _normalize_video_size(payload.get("size"))
    if requested_size is False:
        return {}, _bad_request("`size` must look like `1280x720`.", param="size")

    requested_seconds = _normalize_video_seconds(payload.get("seconds"))
    if requested_seconds is False:
        return {}, _bad_request(
            f"`seconds` must be an integer between {VIDEO_SECONDS_MIN} and "
            f"{VIDEO_SECONDS_MAX} (Doubao's slider range).",
            param="seconds",
        )
    seconds_value = int(requested_seconds) if requested_seconds is not None else None
    if seconds_value is not None and not (
        VIDEO_SECONDS_MIN <= seconds_value <= VIDEO_SECONDS_MAX
    ):
        return {}, _bad_request(
            f"`seconds` must be between {VIDEO_SECONDS_MIN} and "
            f"{VIDEO_SECONDS_MAX}: Doubao exposes exactly those durations.",
            param="seconds",
        )

    references, rejection = _materialize_references(payload, uploads)
    if rejection is not None:
        return {}, rejection
    if references and not resolved.supports_reference_image:
        return {}, _bad_request(
            f"{resolved.id} cannot take a reference image: Doubao's own UI says it "
            "does not support image input for that model. Use "
            + ", ".join(
                model.id
                for model in list_media_models(kind=VIDEO)
                if model.supports_reference_image
            )
            + ".",
            param="model",
        )

    timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
    parsed_timeout = _positive_float(payload.get("timeout"))
    if payload.get("timeout") is not None and parsed_timeout is None:
        return {}, _bad_request(
            "`timeout` must be a positive number of seconds.", param="timeout"
        )
    if parsed_timeout is not None:
        timeout_seconds = min(parsed_timeout, _MAX_TIMEOUT_SECONDS)

    return {
        "prompt": prompt,
        "model": resolved.id,
        "resolved": resolved,
        "size": requested_size,
        "seconds": requested_seconds,
        "seconds_value": seconds_value,
        "references": references,
        "timeout_seconds": timeout_seconds,
    }, None


def _materialize_references(
    payload: dict[str, Any], uploads: list[ReferenceUpload]
) -> tuple[list[Path], Any]:
    """Write the caller's reference assets to a per-request temp directory.

    `references_from_payload` also rejects audio *fields* (it raises rather than
    skipping them), so it belongs inside the same error handling.
    """
    workdir: Path | None = None
    try:
        specs = references_from_payload(payload)
        if not specs and not uploads:
            return [], None
        workdir = Path(tempfile.mkdtemp(prefix="opentoken-video-ref-"))
        return materialize_references(specs, uploads, workdir=workdir), None
    except ReferenceAssetError as exc:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
        return [], _bad_request(str(exc), param=exc.param)


@router.get("/v1/videos")
def videos_list(limit: int = 20):
    """List jobs, newest first, in OpenAI's list envelope."""
    if limit < 1:
        return _bad_request("`limit` must be a positive integer.", param="limit")
    with _JOBS_LOCK:
        _prune_jobs_locked()
        jobs = sorted(
            _JOBS.values(), key=lambda job: job.get("sequence", 0), reverse=True
        )
        snapshots = [dict(job) for job in jobs[: min(limit, 100)]]
    data = [_openai_video_payload(job) for job in snapshots]
    return {
        "object": "list",
        "data": data,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
        "has_more": len(jobs) > len(data),
    }


@router.get("/v1/videos/{video_id}")
def videos_retrieve(video_id: str):
    snapshot = _job_snapshot(video_id)
    if snapshot is None:
        return _job_not_found(video_id)
    return _openai_video_payload(snapshot)


@router.get("/v1/videos/{video_id}/content")
def videos_content(video_id: str):
    """Stream the finished MP4, which is what `videos.download_content()` calls.

    Doubao hands back a signed CDN URL with a long expiry, so this proxies the
    bytes rather than redirecting: the SDK expects an MP4 body, and a 302 to a
    third-party host breaks clients that don't follow cross-origin redirects.
    """
    snapshot = _job_snapshot(video_id)
    if snapshot is None:
        return _job_not_found(video_id)
    url = snapshot.get("url")
    if snapshot["status"] != "completed" or not url:
        return openai_error_response(
            status_code=409,
            message=(
                f"Video {video_id} is not ready (status={snapshot['status']}). "
                "Poll GET /v1/videos/{id} until status is `completed`."
            ),
            error_type="invalid_request_error",
            code="video_not_ready",
        )

    client = httpx.Client(
        timeout=httpx.Timeout(
            connect=_DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
            read=_DOWNLOAD_READ_TIMEOUT_SECONDS,
            write=_DOWNLOAD_READ_TIMEOUT_SECONDS,
            pool=_DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
        ),
        follow_redirects=True,
    )
    try:
        upstream = client.send(client.build_request("GET", str(url)), stream=True)
    except httpx.HTTPError as exc:
        client.close()
        return openai_error_response(
            status_code=502,
            message=f"Failed to fetch the video from the provider ({type(exc).__name__}).",
            error_type="api_error",
        )
    if upstream.status_code >= 400:
        upstream.close()
        client.close()
        return openai_error_response(
            status_code=502,
            message=f"Provider returned {upstream.status_code} for the video file.",
            error_type="api_error",
        )

    def body():
        try:
            for chunk in upstream.iter_bytes():
                yield chunk
        finally:
            upstream.close()
            client.close()

    headers = {"Content-Disposition": f'attachment; filename="{video_id}.mp4"'}
    raw_length = upstream.headers.get("content-length")
    if raw_length:
        headers["Content-Length"] = raw_length
    return StreamingResponse(
        body(),
        media_type=upstream.headers.get("content-type") or "video/mp4",
        headers=headers,
    )


@router.delete("/v1/videos/{video_id}")
def videos_delete(video_id: str):
    """Forget a job. An already-running browser render keeps going; its result is
    dropped when it finishes, because the worker updates the job by id.

    The dump is tombstoned rather than removed: `DELETE` is a client convenience,
    and the file is the record of what Doubao was actually asked to render.
    """
    with _JOBS_LOCK:
        removed = _JOBS.pop(video_id, None)
    if removed is None and _restore_job_from_dump(video_id) is not None:
        with _JOBS_LOCK:
            removed = _JOBS.pop(video_id, None)
    if removed is None:
        return _job_not_found(video_id)
    mark_task_deleted(video_id)
    return {"id": video_id, "object": "video.deleted", "deleted": True}


async def _read_video_body(
    request: Request,
) -> tuple[dict[str, Any], list[ReferenceUpload], Any, dict[str, Any]]:
    """Accept JSON and the SDK's multipart encoding.

    Multipart file parts are handed back separately as
    :class:`ReferenceUpload`s, because a JSON payload can't hold them; JSON
    callers pass `input_reference` as a URL or data URI instead. Audio is
    rejected here rather than deeper down, where the failure would surface as a
    confusing render error.

    The fourth element describes what the body actually contained, for the task
    dump. It is populated even when the request is rejected, because the upload
    bytes are exactly what a post-mortem needs.
    """
    content_type = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        payload: dict[str, Any] = {}
        uploads: list[ReferenceUpload] = []
        files: list[dict[str, Any]] = []
        for key in form.keys():
            value = form[key]
            if hasattr(value, "read"):
                if not is_reference_file_field(key):
                    # Audio has no input at all in the web skill, so name it
                    # explicitly rather than as a generic "unexpected field".
                    if is_audio_field(key):
                        return {}, [], _bad_request(
                            audio_unsupported_message(key), param=key
                        ), _multipart_dump(payload, files)
                    # A file under an unrelated key is a client mistake worth
                    # naming, not something to silently ignore.
                    return {}, [], _bad_request(
                        f"Unexpected file field `{key}`; send files as `input_reference`.",
                        param=key,
                    ), _multipart_dump(payload, files)
                content = await value.read()
                filename = str(getattr(value, "filename", "") or "")
                uploads.append(
                    ReferenceUpload(filename=filename, content=content)
                )
                files.append(
                    {
                        "field": key,
                        "filename": filename,
                        "content_type": str(getattr(value, "content_type", "") or ""),
                        # Base64 rather than raw bytes: JSON can't hold bytes,
                        # and this is the exact file Doubao was handed.
                        "base64": base64.b64encode(content).decode("ascii"),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
            else:
                # A text field naming an audio asset (e.g. `audio=<url>`) is the
                # same refusal as an uploaded one.
                if is_audio_field(key):
                    return {}, [], _bad_request(audio_unsupported_message(key), param=key), _multipart_dump(payload, files)
                payload[key] = value
        # `images` repeated across form parts is allowed; keep them in order.
        if form.getlist("input_reference") and not uploads:
            return {}, [], _bad_request(
                "`input_reference` must be sent as a file part.", param="input_reference"
            ), _multipart_dump(payload, files)
        return payload, uploads, None, _multipart_dump(payload, files)
    try:
        payload = await request.json()
    except Exception:
        return {}, [], _bad_request("Request body must be a JSON object."), {
            "format": "unparseable",
            "body": None,
        }
    if not isinstance(payload, dict):
        return {}, [], _bad_request("Request body must be a JSON object."), {
            "format": "json",
            "body": payload,
        }
    return payload, [], None, {"format": "json", "body": payload}


def _multipart_dump(payload: dict[str, Any], files: list[dict[str, Any]]) -> dict[str, Any]:
    return {"format": "multipart", "fields": payload, "files": files}


def _render_now(
    model: str,
    prompt: str,
    timeout_seconds: float,
    *,
    seconds: int | None = None,
    size: str | None = None,
    references: list[Path] | None = None,
    recorder: TaskRecorder | None = None,
):
    """Run a render inline. Returns `{"asset": ...}` or a ready error response."""
    context = recorder.activate() if recorder is not None else _nullcontext()
    try:
        with context:
            asset = get_default_router().generate_video(
                model=model,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                seconds=seconds,
                size=size,
                reference_images=references or [],
            )
    except ProviderRateLimitError as exc:
        return openai_error_response(
            status_code=429, message=str(exc), error_type="rate_limit_error"
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
    finally:
        _cleanup_references(references or [])
    return {"asset": asset}


def _job_snapshot(job_id: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        _prune_jobs_locked()
        job = _JOBS.get(job_id)
        if job is not None:
            return dict(job)
    return _restore_job_from_dump(job_id)


# A render can be collected by a different process than the one that accepted it
# (the gateway is restarted, or `opentoken start` is re-run) — the Doubao
# conversation still holds the render, but `_JOBS` is per-process. Without this,
# a client polling a successfully submitted job gets `404 video_not_found` and
# reports a failure that never happened.
_INTERRUPTED_MESSAGE = (
    "The gateway restarted while this render was in progress, so its result can "
    "no longer be collected. The request itself reached Doubao; submit it again "
    "to get a video."
)


def _restore_job_from_dump(job_id: str) -> dict[str, Any] | None:
    """Rebuild a job record from `~/.opentoken/tasks/<job_id>.json`.

    A dump still saying `in_progress` means the worker thread died with the old
    process: it is terminal now (the browser session and its poll loop are
    gone), so it is reported as `failed` with an interruption message. Leaving it
    `in_progress` would make clients poll for the full timeout and then give up
    with a vaguer error.
    """
    document = load_task(job_id)
    if document is None:
        return None
    if document.get("deleted_at"):
        # `DELETE /v1/videos/{id}` tombstoned the dump; the file is kept for
        # debugging, but the job must stay gone across restarts.
        return None
    normalized = document.get("normalized") or {}
    status = str(document.get("status") or "in_progress")
    result = document.get("result") or {}
    asset = result.get("asset") if isinstance(result, dict) else None
    payload = result.get("video") if isinstance(result, dict) else None
    if not isinstance(asset, dict) and isinstance(payload, dict):
        asset = {
            "url": payload.get("url"),
            "width": None,
            "height": None,
            "duration_seconds": None,
        }
    asset = asset if isinstance(asset, dict) else {}

    created_at = _dump_timestamp(document.get("created_at"))
    job: dict[str, Any] = {
        "id": job_id,
        "model": normalized.get("model") or "unknown",
        "prompt": normalized.get("prompt"),
        "status": status,
        "created_at": created_at,
        "updated_at": _dump_timestamp(document.get("updated_at"), fallback=created_at),
        "size": normalized.get("size"),
        "seconds": normalized.get("seconds"),
        "reference_images": len(normalized.get("reference_images") or []),
        "restored_from_dump": True,
    }

    if status == "completed" and asset.get("url"):
        job.update(
            {
                "status": "completed",
                "url": asset.get("url"),
                "width": asset.get("width"),
                "height": asset.get("height"),
                "duration_seconds": asset.get("duration_seconds"),
                "completed_at": _dump_timestamp(
                    document.get("updated_at"), fallback=created_at
                ),
            }
        )
    elif status in ("completed", "failed", "rejected"):
        # The dump is terminal but carries no usable asset (a failed render, or
        # one whose result was never written): keep Doubao's own error when there
        # is one so the client sees the real cause.
        error = document.get("error")
        job["status"] = "failed"
        job["error"] = (
            dict(error)
            if isinstance(error, dict) and error.get("message")
            else {"type": "api_error", "message": "Video generation failed."}
        )
    else:
        job["status"] = "failed"
        job["error"] = {
            "type": "api_error",
            "code": "job_interrupted",
            "message": _INTERRUPTED_MESSAGE,
        }
        logger.warning(
            "video_job_restored_as_interrupted job_id=%s dump=%s",
            job_id,
            document.get("updated_at"),
        )

    with _JOBS_LOCK:
        _prune_jobs_locked()
        # Another request may have restored it while this one read the disk.
        existing = _JOBS.get(job_id)
        if existing is not None:
            return dict(existing)
        job["sequence"] = next(_JOB_SEQUENCE)
        _JOBS[job_id] = job
        return dict(job)


def _dump_timestamp(value: Any, *, fallback: int | None = None) -> int:
    """ISO-8601 dump timestamps -> the epoch seconds the OpenAI view uses."""
    from datetime import datetime

    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp())
        except ValueError:
            pass
    return fallback if fallback is not None else int(time())


def _job_not_found(job_id: str):
    return openai_error_response(
        status_code=404,
        message=f"Video generation job not found: {job_id}",
        error_type="invalid_request_error",
        code="video_not_found",
    )


def _normalize_video_size(value: Any) -> str | None | bool:
    """Validate `size` like `1280x720`. Returns the normalized string, None when
    absent, or False when malformed."""
    if value is None:
        return None
    text = str(value).strip().lower().replace(" ", "")
    width, separator, height = text.partition("x")
    if not separator:
        return False
    try:
        parsed_width, parsed_height = int(width), int(height)
    except ValueError:
        return False
    if parsed_width <= 0 or parsed_height <= 0:
        return False
    return f"{parsed_width}x{parsed_height}"


def _normalize_video_seconds(value: Any) -> str | None | bool:
    """Validate `seconds` (OpenAI sends `"4"` / `"8"` / `"12"`). Returns it as a
    string, None when absent, or False when malformed."""
    if value is None:
        return None
    if isinstance(value, bool):
        return False
    text = str(value).strip()
    try:
        parsed = float(text)
    except ValueError:
        return False
    if parsed <= 0:
        return False
    return f"{parsed:g}"


def _openai_video_payload(job: dict[str, Any]) -> dict[str, Any]:
    """Project an internal job onto OpenAI's Video object.

    `size` / `seconds` echo the caller's request because Doubao picks the frame
    size and length itself; until the render lands they stay `None` when the
    caller didn't ask for a specific value, and are filled from the finished
    asset afterwards. `progress` is an elapsed-time estimate (0-99) — upstream
    never reports a percentage.
    """
    completed = job["status"] == "completed"
    payload: dict[str, Any] = {
        "id": job["id"],
        "object": "video",
        "model": job["model"],
        "status": job["status"],
        "progress": _progress_percent(job),
        "created_at": job["created_at"],
        "seconds": job.get("seconds"),
        "size": job.get("size"),
        "remixed_from_video_id": None,
        "error": None,
    }
    if completed:
        width, height = job.get("width"), job.get("height")
        if width and height and not payload["size"]:
            payload["size"] = f"{width}x{height}"
        duration = job.get("duration_seconds")
        if duration and not payload["seconds"]:
            payload["seconds"] = f"{round(duration):g}"
        payload["completed_at"] = job.get("completed_at")
        payload["url"] = job.get("url")
    elif job["status"] == "failed":
        error = job.get("error") or {}
        # Quota exhaustion is a distinct, actionable failure (wait for tomorrow's
        # free tier / switch account) so it gets its own code instead of the
        # catch-all `generation_failed`.
        code = (
            "rate_limit_exceeded"
            if error.get("type") == "rate_limit_error"
            else "generation_failed"
        )
        payload["error"] = {
            # A restored job can name its own cause (e.g. `job_interrupted`), which
            # tells the caller to resubmit rather than debug the prompt.
            "code": str(error.get("code") or code),
            "message": str(error.get("message") or "Video generation failed."),
        }
    return payload


def _progress_percent(job: dict[str, Any]) -> int:
    if job["status"] == "completed":
        return 100
    if job["status"] == "failed":
        return 0
    elapsed = max(0.0, time() - job["created_at"])
    return max(1, min(99, int(elapsed / _EXPECTED_RENDER_SECONDS * 100)))


def _create_job(
    task_id: str,
    model: str,
    prompt: str,
    timeout_seconds: float,
    *,
    size: str | None = None,
    seconds: str | None = None,
    seconds_value: int | None = None,
    references: list[Path] | None = None,
    recorder: TaskRecorder | None = None,
) -> dict[str, Any]:
    job_id = task_id
    references = list(references or [])
    job: dict[str, Any] = {
        "id": job_id,
        "sequence": next(_JOB_SEQUENCE),
        "model": model,
        "prompt": prompt,
        "status": "in_progress",
        "created_at": int(time()),
        "updated_at": int(time()),
        "timeout_seconds": timeout_seconds,
        # Echoed back by the OpenAI view. `size` is projected onto Doubao's ratio
        # buttons and `seconds` onto its 4s–15s slider, so these are the caller's
        # request; the finished asset's real dimensions overwrite nothing here.
        "size": size,
        "seconds": seconds,
        "reference_images": len(references),
    }
    with _JOBS_LOCK:
        _prune_jobs_locked()
        _JOBS[job_id] = job
    # Snapshot before the worker starts: it mutates the stored dict in place, so
    # reading after `start()` raced a fast render and could return "completed"
    # for the creation response.
    created = dict(job)

    worker = threading.Thread(
        target=_run_job,
        args=(job_id, model, prompt, timeout_seconds),
        kwargs={
            "seconds": seconds_value,
            "size": size,
            "references": references,
            "recorder": recorder,
        },
        name=f"video-gen-{job_id}",
        daemon=True,
    )
    worker.start()
    return created


def _run_job(
    job_id: str,
    model: str,
    prompt: str,
    timeout_seconds: float,
    *,
    seconds: int | None = None,
    size: str | None = None,
    references: list[Path] | None = None,
    recorder: TaskRecorder | None = None,
) -> None:
    # `ContextVar`s are not inherited by threads we start ourselves, so the
    # recorder has to be published again here for `doubao_media` to see it.
    context = recorder.activate() if recorder is not None else _nullcontext()
    try:
        with context:
            try:
                asset = get_default_router().generate_video(
                    model=model,
                    prompt=prompt,
                    timeout_seconds=timeout_seconds,
                    seconds=seconds,
                    size=size,
                    reference_images=references or [],
                )
            except Exception as exc:  # noqa: BLE001 - surfaced through the job payload
                message = str(exc)
                # Keep the classification the sync path would have used, so an
                # exhausted quota reads as rate_limit_error (retry later) instead
                # of a generic api_error that looks like a gateway bug.
                if isinstance(exc, ProviderRateLimitError):
                    error_type = "rate_limit_error"
                elif isinstance(exc, RuntimeError):
                    _status_code, error_type = classify_provider_runtime_error(exc)
                else:
                    error_type = "api_error"
                with _JOBS_LOCK:
                    job = _JOBS.get(job_id)
                    if job is not None:
                        job.update(
                            {
                                "status": "failed",
                                "error": {
                                    "message": message,
                                    "type": error_type,
                                },
                                "updated_at": int(time()),
                            }
                        )
                        snapshot = dict(job)
                        snapshot["error"] = dict(job["error"])
                    else:
                        snapshot = None
                if recorder is not None:
                    recorder.finish(
                        status="failed",
                        error={"type": error_type, "message": message},
                        result={
                            "video": _openai_video_payload(snapshot) if snapshot else None
                        },
                    )
                return

            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job is not None:
                    job.update(
                        {
                            "status": "completed",
                            "completed_at": int(time()),
                            "updated_at": int(time()),
                            "url": asset.url,
                            "width": asset.width,
                            "height": asset.height,
                            "duration_seconds": asset.duration_seconds,
                        }
                    )
                    snapshot = dict(job)
                else:
                    snapshot = None
            if recorder is not None:
                recorder.finish(
                    status="completed",
                    result={
                        "asset": _asset_dump(asset),
                        "video": _openai_video_payload(snapshot) if snapshot else None,
                    },
                )
    finally:
        _cleanup_references(references or [])


def _nullcontext():
    """`contextlib.nullcontext` without the import churn at module scope."""
    import contextlib

    return contextlib.nullcontext()


def _cleanup_references(paths: list[Path]) -> None:
    """Remove the per-request temp directory holding materialized references.

    The directory is named `opentoken-video-ref-*` by `_materialize_references`,
    so its parent is what actually gets deleted.
    """
    for path in paths:
        parent = path.parent
        if parent.name.startswith("opentoken-video-ref-"):
            shutil.rmtree(parent, ignore_errors=True)
            return


def _completed_job(
    task_id: str,
    model: str,
    prompt: str,
    timeout_seconds: float,
    asset,
    *,
    size: str | None,
    seconds: str | None,
) -> dict[str, Any]:
    """A job-shaped record for `wait=true`, so the OpenAI view can be reused
    instead of maintaining a second response builder."""
    now = int(time())
    return {
        "id": task_id,
        "sequence": next(_JOB_SEQUENCE),
        "model": model,
        "prompt": prompt,
        "status": "completed",
        "created_at": now,
        "updated_at": now,
        "completed_at": now,
        "timeout_seconds": timeout_seconds,
        "size": size,
        "seconds": seconds,
        "url": asset.url,
        "width": asset.width,
        "height": asset.height,
        "duration_seconds": asset.duration_seconds,
    }


def _completed_payload(model: str, asset) -> dict[str, Any]:
    item: dict[str, Any] = {"url": asset.url}
    if asset.width is not None:
        item["width"] = asset.width
    if asset.height is not None:
        item["height"] = asset.height
    if asset.duration_seconds is not None:
        item["duration"] = asset.duration_seconds
    return {
        "created": int(time()),
        "model": model,
        "data": [item],
    }


def _job_public_payload(job: dict[str, Any], request: Request) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": job["id"],
        "object": "video.generation",
        "status": job["status"],
        "created": job["created_at"],
        "model": job["model"],
        "prompt": job["prompt"],
    }
    if job["status"] == "completed":
        payload["url"] = job.get("url")
        for key in ("width", "height", "duration_seconds"):
            if job.get(key) is not None:
                payload[key] = job[key]
        payload["completed_at"] = job.get("completed_at")
    elif job["status"] == "failed":
        payload["error"] = job.get("error")
    payload["status_url"] = str(
        request.url_for("videos_generations_retrieve", job_id=job["id"])
    )
    return payload


def _prune_jobs_locked() -> None:
    if len(_JOBS) < _JOB_PRUNE_THRESHOLD:
        return
    cutoff = time() - _JOB_TTL_SECONDS
    for job_id in [
        key for key, value in _JOBS.items() if value.get("updated_at", 0) < cutoff
    ]:
        _JOBS.pop(job_id, None)


def _status_for_timeout(status_code: int, exc: Exception) -> int:
    if "timed out" in str(exc).lower():
        return 504
    return status_code


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _as_bool(value: Any, *, default: bool) -> bool:
    """Coerce `wait` without Python's `bool("false") is True` trap.

    JSON clients send real booleans, but shell/curl users write `"wait": "true"`
    or `"wait": "false"` strings; treating every non-empty string as truthy
    would silently start an async job for `"false"`.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("", "0", "false", "no", "off"):
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _bad_request(message: str, *, param: str | None = None):
    return openai_error_response(
        status_code=400,
        message=message,
        error_type="invalid_request_error",
        param=param,
    )


# --------------------------------------------------------------------------- #
# Task dumps (~/.opentoken/tasks/<taskId>.json)
# --------------------------------------------------------------------------- #
def _record_spec(
    recorder: TaskRecorder | None, spec: dict[str, Any]
) -> TaskRecorder | None:
    """Record what the request was normalized into, plus the reference files.

    This is the section that makes a broken image-to-video submission
    diagnosable: it shows the model that was picked, the ratio Doubao's buttons
    will receive, and the exact bytes handed to its file input.
    """
    if recorder is None:
        return None
    resolved = spec["resolved"]
    ratio = None
    if spec["size"]:
        from opentoken.models.media import video_ratio_for_size

        try:
            ratio = video_ratio_for_size(spec["size"])
        except ValueError:
            ratio = None
    references = [_describe_reference(path) for path in spec["references"]]
    recorder.update(
        normalized={
            "model": resolved.id,
            "provider": resolved.provider,
            "model_label": resolved.label,
            "supports_reference_image": resolved.supports_reference_image,
            "prompt": spec["prompt"],
            "size": spec["size"],
            "ratio": ratio,
            "seconds": spec["seconds"],
            "seconds_value": spec["seconds_value"],
            "timeout_seconds": spec["timeout_seconds"],
            "reference_images": references,
        }
    )
    recorder.event(
        "request_normalized",
        model=resolved.id,
        ratio=ratio,
        seconds=spec["seconds_value"],
        reference_images=len(references),
    )
    return recorder


def _describe_reference(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    media_types = describe_reference_media_types([path])
    return {
        "path": str(path),
        "filename": path.name,
        "bytes": len(data),
        "media_type": media_types[0] if media_types else None,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _asset_dump(asset) -> dict[str, Any]:
    return {
        "url": getattr(asset, "url", None),
        "media_type": getattr(asset, "media_type", None),
        "width": getattr(asset, "width", None),
        "height": getattr(asset, "height", None),
        "duration_seconds": getattr(asset, "duration_seconds", None),
        "revised_prompt": getattr(asset, "revised_prompt", None),
    }


def _response_payload(response) -> Any:
    """The JSON body of a route return value (dict or JSONResponse)."""
    body = getattr(response, "body", None)
    if body is None:
        return response
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        try:
            return body.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - a debug dump must not raise
            return str(body)


def _finish_task(
    recorder: TaskRecorder | None,
    *,
    response: Any,
    status: str = "in_progress",
    result: Any = None,
) -> None:
    """Record the POST reply (and, for a sync render, the outcome).

    The default status is `in_progress`: for an async render the worker owns the
    terminal status, and a fast failure there must not be overwritten by this
    later write.
    """
    if recorder is None:
        return
    try:
        payload = _response_payload(response)
        status_code = getattr(response, "status_code", 200)
        recorder.finish(
            status=status,
            response={"status_code": status_code, "body": payload},
            result=result,
        )
    except Exception as exc:  # noqa: BLE001 - never break a render for a dump
        logger.warning("task_dump_finish_failed task_id=%s error=%s", recorder.task_id, exc)


async def _finish_task_async(
    recorder: TaskRecorder | None,
    *,
    response: Any,
    status: str = "in_progress",
    result: Any = None,
) -> None:
    """`_finish_task` for the async route: the write can be tens of megabytes."""
    if recorder is None:
        return
    await run_in_threadpool(
        _finish_task, recorder, response=response, status=status, result=result
    )
