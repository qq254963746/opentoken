"""`~/.opentoken/tasks/<taskId>.json` — the post-mortem trail for a video task.

These files exist to debug "the reference image never reached Doubao", so the
tests care about completeness rather than formatting: the client's exact bytes,
the normalized submit parameters, what the browser flow did, and the outcome.
"""
from __future__ import annotations

import base64
import json
import struct
import threading
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import opentoken.api.routes.videos as videos_route_module
from opentoken.api.app import create_app
from opentoken.providers.base import ProviderRateLimitError
from opentoken.providers.doubao_media import MediaAsset
from opentoken.tasks import recorder as recorder_module
from opentoken.tasks import resolve_tasks_dir

_ASSET = MediaAsset(
    url="https://v11-vdl.doubao.com/video/tos/cn/abc/?mime_type=video_mp4",
    media_type="video/mp4",
    width=1280,
    height=720,
    duration_seconds=10.08,
)


def _png(width: int = 8, height: int = 8) -> bytes:
    raw = b"".join(b"\x00" + bytes([10, 200, 30] * width) for _ in range(height))

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


class FakeRouter:
    def __init__(self, asset=None, error: Exception | None = None, gate=None) -> None:
        self.asset = asset or _ASSET
        self.error = error
        self.gate = gate
        self.calls: list[dict[str, object]] = []

    def generate_video(
        self, *, model, prompt, timeout_seconds, seconds=None, size=None, reference_images=()
    ):
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "seconds": seconds,
                "size": size,
                "reference_images": [str(path) for path in reference_images],
            }
        )
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.error is not None:
            raise self.error
        return self.asset


@pytest.fixture(autouse=True)
def _clear_jobs():
    with videos_route_module._JOBS_LOCK:
        videos_route_module._JOBS.clear()
    yield
    with videos_route_module._JOBS_LOCK:
        videos_route_module._JOBS.clear()


def _use_router(monkeypatch, router: FakeRouter) -> TestClient:
    monkeypatch.setattr(videos_route_module, "get_default_router", lambda: router)
    return TestClient(create_app())


def _dump_path(task_id: str) -> Path:
    return resolve_tasks_dir() / f"{task_id}.json"


def _read_dump(task_id: str) -> dict:
    path = _dump_path(task_id)
    assert path.exists(), f"no task dump at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_for_dump(task_id: str, *, status: str, timeout: float = 5.0) -> dict:
    import time as _time

    deadline = _time.monotonic() + timeout
    document: dict = {}
    while _time.monotonic() < deadline:
        if _dump_path(task_id).exists():
            document = _read_dump(task_id)
            if document.get("status") == status:
                return document
        _time.sleep(0.01)
    raise AssertionError(f"task {task_id} never reached {status}: {document}")


def test_video_task_file_records_the_full_request_and_the_submit_parameters(monkeypatch) -> None:
    """The base64 the client sent must be in the file byte-for-byte.

    The gateway log masks it (deliberately); reproduction needs the original.
    """
    router = FakeRouter()
    client = _use_router(monkeypatch, router)
    encoded = base64.b64encode(_png()).decode()

    response = client.post(
        "/v1/videos",
        json={
            "model": "doubao/seedance-2.0-mini",
            "prompt": "让这只猫跑起来",
            "size": "1280x720",
            "seconds": "8",
            "input_reference": encoded,
        },
    )

    assert response.status_code == 200
    task_id = response.json()["id"]
    document = _wait_for_dump(task_id, status="completed")

    # -- request: exactly what the client sent ------------------------------
    assert document["request"]["method"] == "POST"
    assert document["request"]["path"] == "/v1/videos"
    assert document["request"]["format"] == "json"
    assert document["request"]["body"]["input_reference"] == encoded
    assert document["request"]["body"]["prompt"] == "让这只猫跑起来"

    # -- normalized: what will be pushed into Doubao's UI -------------------
    normalized = document["normalized"]
    assert normalized["model"] == "doubao/seedance-2.0-mini"
    assert normalized["provider"] == "doubao"
    assert normalized["size"] == "1280x720"
    assert normalized["ratio"] == "16:9"  # 1280x720 projected onto Doubao's buttons
    assert normalized["seconds_value"] == 8
    assert normalized["supports_reference_image"] is True
    assert len(normalized["reference_images"]) == 1
    reference = normalized["reference_images"][0]
    assert reference["media_type"] == "image/png"
    assert reference["bytes"] == len(_png())
    assert len(reference["sha256"]) == 64

    # -- response / result --------------------------------------------------
    assert document["response"]["status_code"] == 200
    assert document["response"]["body"]["status"] == "in_progress"
    assert document["result"]["asset"]["url"] == _ASSET.url
    assert document["result"]["asset"]["duration_seconds"] == pytest.approx(10.08)
    assert document["error"] is None


def test_multipart_upload_keeps_the_file_bytes_and_name(monkeypatch) -> None:
    """The SDK posts `input_reference` as a file part; the bytes are the evidence."""
    _use_router(monkeypatch, FakeRouter())
    client = TestClient(create_app())
    png = _png()

    response = client.post(
        "/v1/videos",
        data={"model": "doubao/seedance-2.0", "prompt": "cat", "seconds": "5"},
        files={"input_reference": ("cat.png", png, "image/png")},
    )

    assert response.status_code == 200
    document = _read_dump(response.json()["id"])
    assert document["request"]["format"] == "multipart"
    assert document["request"]["fields"]["prompt"] == "cat"
    stored = document["request"]["files"][0]
    assert stored["field"] == "input_reference"
    assert stored["filename"] == "cat.png"
    assert base64.b64decode(stored["base64"]) == png


def test_a_rejected_request_is_still_dumped(monkeypatch) -> None:
    """"Doubao never submitted" is usually a 400 from this route, not a render
    bug — the rejection reason has to be on disk to tell those apart."""
    _use_router(monkeypatch, FakeRouter())
    client = TestClient(create_app())

    response = client.post(
        "/v1/videos",
        json={"prompt": "cat", "audio": "https://cdn.example/bgm.mp3"},
    )

    assert response.status_code == 400
    tasks = sorted(resolve_tasks_dir().glob("vidgen-*.json"))
    assert len(tasks) == 1
    document = json.loads(tasks[0].read_text(encoding="utf-8"))
    assert document["status"] == "rejected"
    assert document["response"]["status_code"] == 400
    assert "Audio is not supported" in document["response"]["body"]["error"]["message"]
    assert document["request"]["body"]["audio"] == "https://cdn.example/bgm.mp3"


def test_a_failed_render_records_the_provider_error(monkeypatch) -> None:
    _use_router(
        monkeypatch,
        FakeRouter(error=ProviderRateLimitError("今日视频生成免费次数用完了")),
    )
    client = TestClient(create_app())

    response = client.post("/v1/videos", json={"prompt": "cat"})

    assert response.status_code == 200
    document = _wait_for_dump(response.json()["id"], status="failed")
    assert document["error"]["type"] == "rate_limit_error"
    assert "免费次数用完" in document["error"]["message"]
    # `response` is the POST reply (the job was accepted); the terminal state
    # lives in `result`, which is what a poller would have seen.
    assert document["response"]["body"]["status"] == "in_progress"
    assert document["result"]["video"]["status"] == "failed"
    assert document["result"]["video"]["error"]["code"] == "rate_limit_exceeded"


def test_wait_true_returns_the_finished_render_and_dumps_it(monkeypatch) -> None:
    _use_router(monkeypatch, FakeRouter())
    client = TestClient(create_app())

    response = client.post("/v1/videos", json={"prompt": "cat", "wait": True})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    document = _read_dump(body["id"])
    assert document["status"] == "completed"
    assert document["result"]["asset"]["width"] == 1280


def test_the_timeline_carries_the_provider_steps(monkeypatch) -> None:
    """Provider-side steps are recorded through the active recorder, not passed
    down as parameters — this is what proves that wiring works."""
    from opentoken.tasks import record_event, start_task

    recorder = start_task("vidgen-manual", kind="video", request={"format": "json", "body": {}})
    assert recorder is not None
    with recorder.activate():
        record_event("doubao_reference_uploaded", count=1, attachments=[{"alt": "cat.png"}])
        record_event("doubao_prompt_submitted")

    document = _read_dump("vidgen-manual")
    assert [entry["event"] for entry in document["timeline"]] == [
        "doubao_reference_uploaded",
        "doubao_prompt_submitted",
    ]
    assert document["timeline"][0]["attachments"] == [{"alt": "cat.png"}]
    assert document["timeline"][0]["at_ms"] >= 0


def test_threads_need_the_recorder_activated_explicitly(monkeypatch) -> None:
    """A worker thread does not inherit context vars; `_run_job` must re-activate
    or every provider step would be silently dropped from the dump."""
    from opentoken.tasks import active_recorder

    assert active_recorder() is None

    started = _use_router(monkeypatch, FakeRouter())
    response = started.post("/v1/videos", json={"prompt": "cat"})
    assert response.status_code == 200
    # The main thread's context is untouched by the worker.
    assert active_recorder() is None


def test_dumps_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("OPENTOKEN_TASK_DUMPS", "0")
    _use_router(monkeypatch, FakeRouter())
    client = TestClient(create_app())

    response = client.post("/v1/videos", json={"prompt": "cat"})

    assert response.status_code == 200
    assert not list(resolve_tasks_dir().glob("vidgen-*.json"))


def test_a_write_failure_cannot_break_a_render(monkeypatch, tmp_path) -> None:
    """Debug artifacts are best-effort: a read-only tasks dir must not 500."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("occupied", encoding="utf-8")
    monkeypatch.setattr(recorder_module, "resolve_tasks_dir", lambda: blocker)

    _use_router(monkeypatch, FakeRouter())
    client = TestClient(create_app())

    response = client.post("/v1/videos", json={"prompt": "cat"})

    assert response.status_code == 200
    assert response.json()["status"] == "in_progress"


def _simulate_restart() -> None:
    """Drop the in-memory registry, keeping the dump on disk.

    That is exactly what a gateway restart leaves behind — and what made a
    successfully submitted render report `404 video_not_found` to the client
    (task vidgen-2b9d651f: prompt submitted, Doubao said "视频生成中", then the
    process restarted and every later poll 404'd).
    """
    with videos_route_module._JOBS_LOCK:
        videos_route_module._JOBS.clear()


def test_a_poll_after_a_restart_is_answered_from_the_dump(monkeypatch) -> None:
    """A finished render stays retrievable even though `_JOBS` forgot it."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)
    created = client.post("/v1/videos", json={"prompt": "cat", "seconds": "8"}).json()
    task_id = created["id"]
    _wait_for_dump(task_id, status="completed")

    _simulate_restart()

    response = client.get(f"/v1/videos/{task_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["url"] == _ASSET.url
    assert payload["id"] == task_id

    # The restored record is now in the registry, so the list sees it too.
    listed = client.get("/v1/videos").json()["data"]
    assert [item["id"] for item in listed] == [task_id]


def test_an_in_flight_job_interrupted_by_a_restart_fails_with_a_resubmit_hint(
    monkeypatch,
) -> None:
    """`in_progress` in the dump means the worker died with the old process.

    Reporting it as still running would make the client poll for the full timeout
    and then show a vaguer error; a 404 is worse still, since it looks like the
    id was never valid.
    """
    gate = threading.Event()
    router = FakeRouter(gate=gate)
    client = _use_router(monkeypatch, router)
    created = client.post("/v1/videos", json={"prompt": "cat"}).json()
    task_id = created["id"]
    assert client.get(f"/v1/videos/{task_id}").json()["status"] == "in_progress"

    _simulate_restart()

    response = client.get(f"/v1/videos/{task_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "job_interrupted"
    assert "submit it again" in payload["error"]["message"]
    gate.set()


def test_a_restored_job_keeps_doubao_error_message(monkeypatch) -> None:
    """The dump's own failure is what the client needs, not "not found"."""
    client = _use_router(
        monkeypatch, FakeRouter(error=RuntimeError("Doubao's composer rejected the ratio."))
    )
    created = client.post("/v1/videos", json={"prompt": "cat"}).json()
    task_id = created["id"]
    _wait_for_dump(task_id, status="failed")

    _simulate_restart()

    payload = client.get(f"/v1/videos/{task_id}").json()
    assert payload["status"] == "failed"
    assert "composer rejected the ratio" in payload["error"]["message"]


def test_unknown_ids_still_404_and_unusable_ids_are_not_read_as_paths(monkeypatch) -> None:
    """Restoring from disk must not turn the endpoint into a file reader."""
    client = _use_router(monkeypatch, FakeRouter())

    missing = client.get("/v1/videos/vidgen-doesnotexist")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "video_not_found"

    # A traversal-shaped id is rejected by validation before any file is opened.
    escaped = client.get("/v1/videos/..%2F..%2Fconfig")
    assert escaped.status_code == 404
