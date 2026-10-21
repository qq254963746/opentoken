import base64
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import opentoken.api.routes.videos as videos_route_module
from opentoken.api.app import create_app
from opentoken.providers.base import ProviderRateLimitError
from opentoken.providers.doubao_media import MediaAsset

_VIDEO_ASSET = MediaAsset(
    url="https://v11-vdl.doubao.com/video/tos/cn/abc/?mime_type=video_mp4",
    media_type="video/mp4",
    width=1280,
    height=720,
    duration_seconds=10.08,
)


class FakeRouter:
    def __init__(self, asset=None, error: Exception | None = None, gate=None) -> None:
        self.asset = asset or _VIDEO_ASSET
        self.error = error
        self.gate = gate
        self.calls: list[dict[str, object]] = []

    def generate_video(
        self,
        *,
        model,
        prompt,
        timeout_seconds,
        seconds=None,
        size=None,
        reference_images=(),
    ):
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "timeout_seconds": timeout_seconds,
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


def _use_router(monkeypatch, router: FakeRouter) -> TestClient:
    monkeypatch.setattr(videos_route_module, "get_default_router", lambda: router)
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _clear_jobs():
    with videos_route_module._JOBS_LOCK:
        videos_route_module._JOBS.clear()
    yield
    with videos_route_module._JOBS_LOCK:
        videos_route_module._JOBS.clear()


def _poll_until_done(client: TestClient, job_id: str, *, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    payload: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/videos/generations/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in ("completed", "failed"):
            return payload
        time.sleep(0.01)
    raise AssertionError(f"job never settled: {payload}")


def test_videos_generations_wait_true_returns_the_finished_render(monkeypatch) -> None:
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos/generations",
        json={
            "prompt": "一只小猫在草地上奔跑",
            "model": "doubao/seedance-2.0-mini",
            "wait": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] == "doubao/seedance-2.0-mini"
    assert payload["data"] == [
        {
            "url": _VIDEO_ASSET.url,
            "width": 1280,
            "height": 720,
            "duration": 10.08,
        }
    ]
    assert router.calls == [
        {
            "model": "doubao/seedance-2.0-mini",
            "prompt": "一只小猫在草地上奔跑",
            "timeout_seconds": 600.0,
            "seconds": None,
            "size": None,
            "reference_images": [],
        }
    ]


def test_videos_generations_defaults_to_the_default_model(monkeypatch) -> None:
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post("/v1/videos/generations", json={"prompt": "一只猫", "wait": True})

    assert response.status_code == 200
    assert router.calls[0]["model"] == "doubao/seedance-2.0-mini"


def test_videos_generations_async_job_can_be_polled(monkeypatch) -> None:
    gate = threading.Event()
    router = FakeRouter(gate=gate)
    client = _use_router(monkeypatch, router)

    response = client.post("/v1/videos/generations", json={"prompt": "一只小猫"})

    assert response.status_code == 200
    job = response.json()
    assert job["object"] == "video.generation"
    assert job["status"] == "in_progress"
    assert job["prompt"] == "一只小猫"
    assert job["status_url"] == f"http://testserver/v1/videos/generations/{job['id']}"

    pending = client.get(f"/v1/videos/generations/{job['id']}").json()
    assert pending["status"] == "in_progress"

    gate.set()
    finished = _poll_until_done(client, job["id"])

    assert finished["status"] == "completed"
    assert finished["url"] == _VIDEO_ASSET.url
    assert finished["width"] == 1280
    assert finished["duration_seconds"] == 10.08
    assert isinstance(finished["completed_at"], int)


def test_videos_generations_async_job_surfaces_failures(monkeypatch) -> None:
    client = _use_router(monkeypatch, FakeRouter(error=RuntimeError("generation exploded")))

    job = client.post("/v1/videos/generations", json={"prompt": "一只小猫"}).json()
    finished = _poll_until_done(client, job["id"])

    assert finished["status"] == "failed"
    assert finished["error"] == {"message": "generation exploded", "type": "api_error"}


def test_videos_generations_retrieve_returns_404_for_unknown_jobs(monkeypatch) -> None:
    client = _use_router(monkeypatch, FakeRouter())

    response = client.get("/v1/videos/generations/vidgen-missing")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"prompt": "  "},
        {"prompt": "x", "model": "doubao/seedance-9.9"},
        {"prompt": "x", "timeout": 0},
        {"prompt": "x", "timeout": "later"},
    ],
)
def test_videos_generations_rejects_bad_requests(monkeypatch, body) -> None:
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post("/v1/videos/generations", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert router.calls == []


def test_videos_generations_caps_the_timeout(monkeypatch) -> None:
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos/generations",
        json={"prompt": "一只猫", "wait": True, "timeout": 99_999},
    )

    assert response.status_code == 200
    assert router.calls[0]["timeout_seconds"] == 900.0


def test_videos_generations_maps_rate_limits(monkeypatch) -> None:
    client = _use_router(monkeypatch, FakeRouter(error=ProviderRateLimitError("slow down")))

    response = client.post("/v1/videos/generations", json={"prompt": "一只猫", "wait": True})

    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"


def test_videos_generations_maps_timeouts_to_504(monkeypatch) -> None:
    client = _use_router(
        monkeypatch,
        FakeRouter(error=RuntimeError("Doubao video generation timed out after 60s")),
    )

    response = client.post("/v1/videos/generations", json={"prompt": "一只猫", "wait": True})

    assert response.status_code == 504


@pytest.mark.parametrize("wait_value", [True, "true", "TRUE", 1, "1", "yes"])
def test_videos_generations_treats_truthy_wait_forms_as_synchronous(
    monkeypatch, wait_value
) -> None:
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos/generations", json={"prompt": "一只猫", "wait": wait_value}
    )

    assert response.status_code == 200
    assert "data" in response.json()
    assert len(router.calls) == 1


@pytest.mark.parametrize("wait_value", [False, "false", "FALSE", 0, "0", "no", "", None])
def test_videos_generations_does_not_block_for_falsy_wait_forms(
    monkeypatch, wait_value
) -> None:
    gate = threading.Event()
    router = FakeRouter(gate=gate)
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos/generations", json={"prompt": "一只猫", "wait": wait_value}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "in_progress"
    gate.set()


# --------------------------------------------------------------------------- #
# OpenAI Sora shape: /v1/videos
# --------------------------------------------------------------------------- #
def test_videos_create_returns_an_openai_video_object(monkeypatch) -> None:
    """This is the path the official SDK's `client.videos.create()` posts to.

    It used to answer 404 {"detail": "Not Found"} because only
    `/v1/videos/generations` was registered.
    """
    gate = threading.Event()
    router = FakeRouter(gate=gate)
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos",
        json={
            "model": "doubao/seedance-2.0-mini",
            "prompt": "一只小猫在草地上奔跑",
            "size": "1280x720",
            "seconds": "8",
        },
    )

    assert response.status_code == 200
    video = response.json()
    assert video["object"] == "video"
    assert video["status"] == "in_progress"
    assert video["model"] == "doubao/seedance-2.0-mini"
    assert video["size"] == "1280x720"
    assert video["seconds"] == "8"
    assert video["remixed_from_video_id"] is None
    assert video["error"] is None
    assert 1 <= video["progress"] <= 99
    assert video["id"].startswith("vidgen-")
    gate.set()


def test_videos_retrieve_and_content_serve_the_finished_render(monkeypatch) -> None:
    gate = threading.Event()
    client = _use_router(monkeypatch, FakeRouter(gate=gate))

    created = client.post("/v1/videos", json={"prompt": "一只小猫"}).json()
    video_id = created["id"]
    # No size/seconds requested -> null until the render tells us the truth.
    assert created["size"] is None
    assert created["seconds"] is None

    polled = client.get(f"/v1/videos/{video_id}").json()
    assert polled["status"] == "in_progress"

    not_ready = client.get(f"/v1/videos/{video_id}/content")
    assert not_ready.status_code == 409

    gate.set()
    finished = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        finished = client.get(f"/v1/videos/{video_id}").json()
        if finished["status"] == "completed":
            break
        time.sleep(0.01)

    assert finished is not None
    assert finished["status"] == "completed"
    assert finished["progress"] == 100
    assert finished["size"] == "1280x720"
    assert finished["seconds"] == "10"
    assert finished["completed_at"] is not None
    assert finished["url"] == _VIDEO_ASSET.url


def test_videos_content_streams_the_provider_bytes(monkeypatch) -> None:
    import httpx

    client = _use_router(monkeypatch, FakeRouter())
    video_id = client.post("/v1/videos", json={"prompt": "一只小猫"}).json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get(f"/v1/videos/{video_id}").json()["status"] == "completed":
            break
        time.sleep(0.01)

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200, headers={"content-type": "video/mp4"}, content=b"MP4BYTES"
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        videos_route_module.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    response = client.get(f"/v1/videos/{video_id}/content")

    assert response.status_code == 200
    assert response.content == b"MP4BYTES"
    assert response.headers["content-type"].startswith("video/mp4")
    assert f'filename="{video_id}.mp4"' in response.headers["content-disposition"]
    assert seen["url"] == _VIDEO_ASSET.url


def test_videos_content_is_409_until_the_render_lands(monkeypatch) -> None:
    gate = threading.Event()
    client = _use_router(monkeypatch, FakeRouter(gate=gate))

    video_id = client.post("/v1/videos", json={"prompt": "一只小猫"}).json()["id"]

    response = client.get(f"/v1/videos/{video_id}/content")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "video_not_ready"
    gate.set()


def test_videos_content_reports_provider_failures(monkeypatch) -> None:
    import httpx

    client = _use_router(monkeypatch, FakeRouter())
    video_id = client.post("/v1/videos", json={"prompt": "一只小猫"}).json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get(f"/v1/videos/{video_id}").json()["status"] == "completed":
            break
        time.sleep(0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"expired")

    real_client = httpx.Client
    monkeypatch.setattr(
        videos_route_module.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    response = client.get(f"/v1/videos/{video_id}/content")

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "api_error"


def test_videos_list_returns_the_openai_list_envelope(monkeypatch) -> None:
    gate = threading.Event()
    client = _use_router(monkeypatch, FakeRouter(gate=gate))

    first = client.post("/v1/videos", json={"prompt": "一只猫"}).json()
    second = client.post("/v1/videos", json={"prompt": "一只狗"}).json()

    payload = client.get("/v1/videos").json()

    assert payload["object"] == "list"
    ids = [item["id"] for item in payload["data"]]
    assert ids[:2] == [second["id"], first["id"]]
    assert payload["first_id"] == second["id"]
    assert payload["has_more"] is False

    limited = client.get("/v1/videos?limit=1").json()
    assert [item["id"] for item in limited["data"]] == [second["id"]]
    assert limited["has_more"] is True
    gate.set()


def test_videos_wait_returns_the_finished_video_object(monkeypatch) -> None:
    """`wait` is an extension, but it must keep the OpenAI envelope."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos", json={"prompt": "一只猫", "wait": True, "timeout": 30}
    )

    assert response.status_code == 200
    video = response.json()
    assert video["object"] == "video"
    assert video["status"] == "completed"
    assert video["progress"] == 100
    assert video["url"] == _VIDEO_ASSET.url
    assert router.calls[0]["timeout_seconds"] == 30.0


def test_videos_create_accepts_the_sdk_multipart_encoding(monkeypatch) -> None:
    """openai-python posts multipart/form-data for videos.create()."""
    gate = threading.Event()
    router = FakeRouter(gate=gate)
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos",
        data={"prompt": "一只小猫", "model": "doubao/seedance-2.0-mini", "seconds": "4"},
    )

    assert response.status_code == 200
    video = response.json()
    assert video["seconds"] == "4"
    assert video["status"] == "in_progress"
    assert router.calls[0]["prompt"] == "一只小猫"
    gate.set()


_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00"
    b"\x18\xdd\x8d\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_videos_create_accepts_a_reference_image(monkeypatch) -> None:
    """A reference image becomes a real file for the browser to upload.

    The gateway used to reject `input_reference` outright on the (wrong) belief
    that the web skill was text-to-video only; Doubao's own upload input takes
    images, so the file is materialized and handed to the renderer.
    """
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos",
        data={"prompt": "让这只猫跑起来", "model": "doubao/seedance-2.0-mini"},
        files={"input_reference": ("cat.png", _TINY_PNG, "image/png")},
    )

    assert response.status_code == 200
    assert len(router.calls) == 1
    references = router.calls[0]["reference_images"]
    assert len(references) == 1
    # The path handed to the renderer must exist while the job runs, and the
    # worker cleans the temp directory up afterwards.
    assert references[0].endswith(".png")
    assert Path(references[0]).suffix == ".png"


def test_videos_create_accepts_a_reference_image_as_a_data_uri(monkeypatch) -> None:
    """JSON callers have no file part, so a data URI has to work as well."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)
    encoded = base64.b64encode(_TINY_PNG).decode()

    response = client.post(
        "/v1/videos",
        json={
            "prompt": "让这只猫跑起来",
            "input_reference": f"data:image/png;base64,{encoded}",
        },
    )

    assert response.status_code == 200
    assert len(router.calls[0]["reference_images"]) == 1


def test_videos_create_rejects_audio_references(monkeypatch) -> None:
    """Audio cannot be attached: the skill's input is image-only, and forcing a
    .wav through it makes the UI treat the audio as a (broken) image."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos",
        data={"prompt": "一只小猫"},
        files={"input_reference": ("soundtrack.wav", b"RIFF\x00\x00\x00\x00WAVE", "audio/wav")},
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["param"] == "input_reference"
    assert "audio" in error["message"].lower()
    assert router.calls == []


def test_videos_create_rejects_a_reference_image_for_a_model_without_support(
    monkeypatch,
) -> None:
    """Doubao's UI itself says Seedance 2.0 Fast cannot take a reference image."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos",
        data={"prompt": "让这只猫跑起来", "model": "doubao/seedance-2.0-fast"},
        files={"input_reference": ("cat.png", _TINY_PNG, "image/png")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "model"
    assert router.calls == []


def test_videos_create_passes_seconds_and_size_to_the_renderer(monkeypatch) -> None:
    """`seconds` and `size` are no longer echo-only: they drive the UI controls."""
    gate = threading.Event()
    router = FakeRouter(gate=gate)
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos",
        json={"prompt": "一只猫", "seconds": "12", "size": "720x1280"},
    )

    assert response.status_code == 200
    assert router.calls[0]["seconds"] == 12
    assert router.calls[0]["size"] == "720x1280"
    gate.set()


def test_videos_create_rejects_seconds_outside_doubaos_slider(monkeypatch) -> None:
    """Doubao offers 4s–15s only; clamping silently would return a different clip."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    for seconds in ("3", "16", "0", "-5"):
        response = client.post("/v1/videos", json={"prompt": "一只猫", "seconds": seconds})
        assert response.status_code == 400, seconds
        assert response.json()["error"]["param"] == "seconds"
    assert router.calls == []


def test_videos_delete_forgets_the_job(monkeypatch) -> None:
    gate = threading.Event()
    client = _use_router(monkeypatch, FakeRouter(gate=gate))
    video_id = client.post("/v1/videos", json={"prompt": "一只猫"}).json()["id"]

    deleted = client.delete(f"/v1/videos/{video_id}")

    assert deleted.status_code == 200
    assert deleted.json() == {
        "id": video_id,
        "object": "video.deleted",
        "deleted": True,
    }
    assert client.get(f"/v1/videos/{video_id}").status_code == 404
    gate.set()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"prompt": "  "},
        {"prompt": "x", "model": "doubao/seedance-9.9"},
        {"prompt": "x", "size": "huge"},
        {"prompt": "x", "size": "1280"},
        {"prompt": "x", "seconds": "0"},
        {"prompt": "x", "seconds": "soon"},
        {"prompt": "x", "timeout": 0},
    ],
)
def test_videos_create_rejects_bad_requests(monkeypatch, body) -> None:
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post("/v1/videos", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert router.calls == []


def test_videos_retrieve_maps_failed_jobs_to_the_openai_error_object(monkeypatch) -> None:
    client = _use_router(monkeypatch, FakeRouter(error=RuntimeError("boom")))

    video_id = client.post("/v1/videos", json={"prompt": "一只猫"}).json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        video = client.get(f"/v1/videos/{video_id}").json()
        if video["status"] == "failed":
            break
        time.sleep(0.01)

    assert video["status"] == "failed"
    assert video["progress"] == 0
    assert video["error"] == {"code": "generation_failed", "message": "boom"}


def test_videos_retrieve_unknown_id_is_404(monkeypatch) -> None:
    client = _use_router(monkeypatch, FakeRouter())

    response = client.get("/v1/videos/video_missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "video_not_found"


def test_videos_async_job_reports_an_exhausted_quota_as_a_rate_limit(monkeypatch) -> None:
    """A quota failure is actionable (retry tomorrow / other account), so it must
    not be flattened into the generic `api_error` that looks like a gateway bug."""
    client = _use_router(
        monkeypatch,
        FakeRouter(
            error=ProviderRateLimitError(
                "Doubao media quota exhausted: 今日视频生成免费次数用完了…"
            )
        ),
    )

    job = client.post("/v1/videos/generations", json={"prompt": "一只小猫"}).json()
    finished = _poll_until_done(client, job["id"])

    assert finished["status"] == "failed"
    assert finished["error"]["type"] == "rate_limit_error"
    assert "quota exhausted" in finished["error"]["message"]


def test_videos_openai_view_uses_a_distinct_code_for_an_exhausted_quota(monkeypatch) -> None:
    client = _use_router(
        monkeypatch,
        FakeRouter(error=ProviderRateLimitError("Doubao media quota exhausted: 今日…")),
    )

    video_id = client.post("/v1/videos", json={"prompt": "一只小猫"}).json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        video = client.get(f"/v1/videos/{video_id}").json()
        if video["status"] == "failed":
            break
        time.sleep(0.01)

    assert video["status"] == "failed"
    assert video["error"]["code"] == "rate_limit_exceeded"


def test_videos_openai_view_keeps_generation_failed_for_other_errors(monkeypatch) -> None:
    client = _use_router(monkeypatch, FakeRouter(error=RuntimeError("boom")))

    video_id = client.post("/v1/videos", json={"prompt": "一只小猫"}).json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        video = client.get(f"/v1/videos/{video_id}").json()
        if video["status"] == "failed":
            break
        time.sleep(0.01)

    assert video["error"]["code"] == "generation_failed"


def test_videos_create_rejects_audio_fields_by_name(monkeypatch) -> None:
    """An `audio` field must fail loudly, never be dropped silently.

    Doubao's web video skill generates silent video; a caller who passed a
    soundtrack and got a 200 would have no way to know it was ignored.
    """
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    for field in ("audio", "audios", "audio_url", "music", "soundtrack", "bgm"):
        response = client.post(
            "/v1/videos",
            json={"prompt": "一只小猫", field: "https://cdn.example/bgm.mp3"},
        )
        assert response.status_code == 400, field
        error = response.json()["error"]
        assert error["param"] == field
        assert "audio is not supported" in error["message"].lower()
    assert router.calls == []


def test_videos_create_rejects_an_audio_file_part(monkeypatch) -> None:
    """The multipart form may name the audio part instead of using input_reference."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos",
        data={"prompt": "一只小猫"},
        files={"audio": ("bgm.wav", b"RIFF\x00\x00\x00\x00WAVE", "audio/wav")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "audio"
    assert router.calls == []


def test_videos_create_rejects_an_audio_value_in_a_form_field(monkeypatch) -> None:
    """A URL passed as a *text* field is refused the same way as an upload."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos",
        data={"prompt": "一只小猫", "audio_url": "https://cdn.example/bgm.mp3"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "audio_url"
    assert router.calls == []


def test_videos_create_rejects_audio_disguised_as_a_png(monkeypatch) -> None:
    """Detection is by content, not by extension or declared content type."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos",
        data={"prompt": "一只小猫"},
        files={"input_reference": ("cat.png", b"ID3\x04\x00\x00\x00\x00\x00\x00", "image/png")},
    )

    assert response.status_code == 400
    assert "audio" in response.json()["error"]["message"].lower()
    assert router.calls == []


def test_videos_create_rejects_video_references(monkeypatch) -> None:
    """Video reference is refused too, with its own explanation."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16

    response = client.post(
        "/v1/videos",
        data={"prompt": "一只小猫"},
        files={"input_reference": ("clip.mp4", mp4, "video/mp4")},
    )

    assert response.status_code == 400
    assert "video input is not supported" in response.json()["error"]["message"].lower()
    assert router.calls == []


def test_videos_create_accepts_bare_base64_without_a_data_uri_prefix(monkeypatch) -> None:
    """Clients often pass raw base64 (a copied `b64_json`), not a data URI."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)
    bare = base64.b64encode(_TINY_PNG).decode()

    response = client.post(
        "/v1/videos",
        json={"prompt": "让这只猫跑起来", "input_reference": bare},
    )

    assert response.status_code == 200
    references = router.calls[0]["reference_images"]
    assert len(references) == 1
    assert references[0].endswith(".png")


def test_videos_create_accepts_base64_wrapped_across_lines(monkeypatch) -> None:
    """base64url + MIME line wrapping both appear in real client output."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)
    encoded = base64.urlsafe_b64encode(_TINY_PNG).decode()
    wrapped = "\n".join(encoded[i : i + 76] for i in range(0, len(encoded), 76))

    response = client.post(
        "/v1/videos",
        json={"prompt": "让这只猫跑起来", "input_reference": wrapped},
    )

    assert response.status_code == 200
    assert len(router.calls[0]["reference_images"]) == 1


def test_videos_create_rejects_a_base64_body_that_is_not_media(monkeypatch) -> None:
    """Only decodable *media* counts; a random token must not become a file."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/videos",
        json={
            "prompt": "一只猫",
            "input_reference": base64.b64encode(b"hello world").decode(),
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "input_reference"
    assert router.calls == []


def test_videos_create_rejects_base64_audio_with_the_audio_explanation(monkeypatch) -> None:
    """A base64 wav must fail as audio, not as an unrecognised image."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)
    wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 20

    response = client.post(
        "/v1/videos",
        json={"prompt": "一只猫", "input_reference": base64.b64encode(wav).decode()},
    )

    assert response.status_code == 400
    assert "audio is not supported" in response.json()["error"]["message"].lower()
    assert router.calls == []
