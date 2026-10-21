import base64

import httpx
import pytest
from fastapi.testclient import TestClient

import opentoken.api.routes.images as images_route_module
from opentoken.api.app import create_app
from opentoken.providers.base import ProviderRateLimitError
from opentoken.providers.doubao_media import MediaAsset


class FakeRouter:
    def __init__(self, assets=None, error: Exception | None = None) -> None:
        self.assets = assets or []
        self.error = error
        self.calls: list[dict[str, object]] = []

    def generate_images(self, *, model, prompt, count, timeout_seconds):
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "count": count,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        return self.assets


def _use_router(monkeypatch, router: FakeRouter) -> TestClient:
    monkeypatch.setattr(images_route_module, "get_default_router", lambda: router)
    return TestClient(create_app())


def test_images_generations_returns_openai_style_urls(monkeypatch) -> None:
    router = FakeRouter(
        assets=[
            MediaAsset(
                url="https://cdn.example/a~image_raw.png",
                media_type="image/png",
                width=2048,
                height=2048,
                revised_prompt="重写后的提示词",
            ),
            MediaAsset(url="https://cdn.example/b~image_raw.png", media_type="image/png"),
        ]
    )
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/images/generations",
        json={"prompt": "一只猫", "n": 2, "model": "doubao/seedream-5.0-flash"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] == "doubao/seedream-5.0-flash"
    assert isinstance(payload["created"], int)
    assert payload["data"] == [
        {
            "url": "https://cdn.example/a~image_raw.png",
            "revised_prompt": "重写后的提示词",
        },
        {"url": "https://cdn.example/b~image_raw.png"},
    ]
    assert router.calls == [
        {
            "model": "doubao/seedream-5.0-flash",
            "prompt": "一只猫",
            "count": 2,
            "timeout_seconds": 240.0,
        }
    ]


def test_images_generations_defaults_to_the_default_model(monkeypatch) -> None:
    router = FakeRouter(assets=[MediaAsset(url="https://cdn.example/a.png", media_type="image/png")])
    client = _use_router(monkeypatch, router)

    response = client.post("/v1/images/generations", json={"prompt": "一只猫"})

    assert response.status_code == 200
    assert router.calls[0]["model"] == "doubao/seedream-5.0-flash"
    assert router.calls[0]["count"] == 1


def test_images_generations_caps_n(monkeypatch) -> None:
    router = FakeRouter(assets=[MediaAsset(url="https://cdn.example/a.png", media_type="image/png")])
    client = _use_router(monkeypatch, router)

    response = client.post("/v1/images/generations", json={"prompt": "一只猫", "n": 32})

    assert response.status_code == 200
    assert router.calls[0]["count"] == 4


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"prompt": "   "},
        {"prompt": "x", "n": 0},
        {"prompt": "x", "n": "many"},
        {"prompt": "x", "model": "doubao/seedream-1.0"},
        {"prompt": "x", "response_format": "bytes"},
        {"prompt": "x", "timeout": -1},
    ],
)
def test_images_generations_rejects_bad_requests(monkeypatch, body) -> None:
    router = FakeRouter(assets=[MediaAsset(url="https://cdn.example/a.png", media_type="image/png")])
    client = _use_router(monkeypatch, router)

    response = client.post("/v1/images/generations", json=body)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert router.calls == []


def test_images_generations_returns_b64_json(monkeypatch) -> None:
    router = FakeRouter(
        assets=[MediaAsset(url="https://cdn.example/a~image_raw.png", media_type="image/png")]
    )
    client = _use_router(monkeypatch, router)
    payload = b"\x89PNG\r\n\x1a\nfake"

    def fake_get(url, timeout=None, follow_redirects=False):
        assert url == "https://cdn.example/a~image_raw.png"
        assert follow_redirects is True
        return httpx.Response(200, content=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(images_route_module.httpx, "get", fake_get)

    response = client.post(
        "/v1/images/generations",
        json={"prompt": "一只猫", "response_format": "b64_json"},
    )

    assert response.status_code == 200
    item = response.json()["data"][0]
    assert set(item.keys()) == {"b64_json"}
    assert base64.b64decode(item["b64_json"]) == payload


def test_images_generations_maps_download_failures_to_502(monkeypatch) -> None:
    router = FakeRouter(
        assets=[MediaAsset(url="https://cdn.example/a~image_raw.png", media_type="image/png")]
    )
    client = _use_router(monkeypatch, router)

    def fake_get(url, timeout=None, follow_redirects=False):
        raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

    monkeypatch.setattr(images_route_module.httpx, "get", fake_get)

    response = client.post(
        "/v1/images/generations",
        json={"prompt": "一只猫", "response_format": "b64_json"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "api_error"


def test_images_generations_maps_rate_limits(monkeypatch) -> None:
    client = _use_router(monkeypatch, FakeRouter(error=ProviderRateLimitError("slow down")))

    response = client.post("/v1/images/generations", json={"prompt": "一只猫"})

    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"


def test_images_generations_maps_timeouts_to_504(monkeypatch) -> None:
    client = _use_router(
        monkeypatch,
        FakeRouter(error=RuntimeError("Doubao image generation timed out after 30s")),
    )

    response = client.post("/v1/images/generations", json={"prompt": "一只猫", "timeout": 30})

    assert response.status_code == 504


def test_images_generations_reports_an_empty_provider_result(monkeypatch) -> None:
    client = _use_router(monkeypatch, FakeRouter(assets=[]))

    response = client.post("/v1/images/generations", json={"prompt": "一只猫"})

    assert response.status_code == 502
    assert "no generated images" in response.json()["error"]["message"]


def test_images_generations_rejects_audio_fields_by_name(monkeypatch) -> None:
    """Audio is refused explicitly; the image skill is text-to-image only."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    for field in ("audio", "audio_url", "music", "soundtrack", "bgm"):
        response = client.post(
            "/v1/images/generations",
            json={"prompt": "一只小猫", field: "https://cdn.example/bgm.mp3"},
        )
        assert response.status_code == 400, field
        error = response.json()["error"]
        assert error["param"] == field
        assert "audio is not supported" in error["message"].lower()
    assert router.calls == []


def test_images_generations_rejects_reference_images(monkeypatch) -> None:
    """Silently ignoring a source image would return an unrelated picture."""
    router = FakeRouter()
    client = _use_router(monkeypatch, router)

    response = client.post(
        "/v1/images/generations",
        json={"prompt": "一只小猫", "image": "https://cdn.example/cat.png"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "image"
    assert router.calls == []
