from fastapi.testclient import TestClient

from opentoken.api.app import create_app
from opentoken.models.capabilities import CAPABILITIES, IMAGE, TEXT, VIDEO


def test_models_endpoint_returns_openai_style_list() -> None:
    client = TestClient(create_app())

    response = client.get("/v1/models")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == {"object", "data"}
    assert payload["object"] == "list"
    assert isinstance(payload["data"], list)
    for item in payload["data"]:
        assert set(item.keys()) == {"id", "object", "owned_by", "capability"}
        assert item["object"] == "model"
        # Chat models are text; media models (Seedream/Seedance) advertise their
        # own output type, so only the enum is asserted here.
        assert item["capability"] in CAPABILITIES, item
        assert item["owned_by"] == "opentoken"
        # ONE format only: bare `<provider>/<model>` — never the `algae/` prefix.
        assert not item["id"].startswith("algae/"), item["id"]


def test_models_endpoint_lists_only_bare_provider_model_ids() -> None:
    """/v1/models must advertise a single, bare `<provider>/<model>` form — no
    `algae/…` namespaced ids, and no namespace-less raw model names (which would
    be ambiguous across providers, e.g. glm-5)."""
    client = TestClient(create_app())
    payload = client.get("/v1/models").json()
    for item in payload["data"]:
        mid = item["id"]
        assert not mid.startswith("algae/"), mid
        # at least one slash → it carries a provider segment
        assert "/" in mid, mid


def test_models_endpoint_omits_retired_or_duplicate_ids() -> None:
    client = TestClient(create_app())

    response = client.get("/v1/models")

    assert response.status_code == 200
    model_ids = {item["id"] for item in response.json()["data"]}

    # These ids used to leak from the hardcoded catalog. Now that the catalog is
    # live-discovered, they should not reappear unless a provider's upstream
    # explicitly lists them.
    retired = {
        "qwen-intl/qwen3.5-turbo",
        "qwen-cn/qwen3.5-plus",
        "qwen-cn/qwen3.5-turbo",
        "qwen-cn/Qwen3.5-Plus",
        "qwen-cn/Qwen3.5-Turbo",
        "doubao/doubao-lite",
        "glm-cn/glm-4",
        "glm-cn/glm-4-zero",
        "mimo/mimo-v2-pro",
        "mimo/xiaomimo-chat",
    }

    assert retired.isdisjoint(model_ids)


def test_models_endpoint_does_not_advertise_unimplemented_embedding_models() -> None:
    """/v1/embeddings 永久 501,所以 /v1/models 也不该再把 text-embedding-* 列
    出来 —— 之前的"故意 decouple"会让 SDK auto-discover 拿了再调 → 51X 错误。
    既然端点不可用,model 名也不暴露。"""
    client = TestClient(create_app())

    response = client.get("/v1/models")

    assert response.status_code == 200
    model_ids = {item["id"] for item in response.json()["data"]}

    assert "text-embedding-3-small" not in model_ids
    assert "text-embedding-3-large" not in model_ids
    assert "text-embedding-ada-002" not in model_ids


def test_models_endpoint_advertises_media_models_for_logged_in_providers(monkeypatch) -> None:
    import opentoken.api.routes.models as models_route_module
    from opentoken.models.media import resolve_media_model

    image_model = resolve_media_model("doubao/seedream-5.0-flash", kind="image")
    video_model = resolve_media_model("doubao/seedance-2.0-mini", kind="video")
    assert image_model is not None and video_model is not None
    monkeypatch.setattr(
        models_route_module,
        "media_models_for_logged_in_providers",
        lambda: [image_model, video_model],
    )

    payload = TestClient(create_app()).get("/v1/models").json()

    ids = [item["id"] for item in payload["data"]]
    assert ids.count("doubao/seedream-5.0-flash") == 1
    assert ids.count("doubao/seedance-2.0-mini") == 1
    entry = next(item for item in payload["data"] if item["id"] == "doubao/seedream-5.0-flash")
    assert entry["object"] == "model"
    assert entry["owned_by"] == "opentoken"
    # A Seedream entry must not be mistaken for a chat model.
    assert entry["capability"] == IMAGE
    video_entry = next(
        item for item in payload["data"] if item["id"] == "doubao/seedance-2.0-mini"
    )
    assert video_entry["capability"] == VIDEO


def test_models_endpoint_marks_chat_models_as_text(monkeypatch) -> None:
    import opentoken.api.routes.models as models_route_module
    from opentoken.models.catalog import ModelCatalogEntry

    monkeypatch.setattr(
        models_route_module,
        "get_default_model_registry",
        lambda: type(
            "_Registry",
            (),
            {"list_models": lambda self: [ModelCatalogEntry("algae/x/model-a", "opentoken", "Model A")]},
        )(),
    )
    monkeypatch.setattr(
        models_route_module, "media_models_for_logged_in_providers", lambda: []
    )

    payload = TestClient(create_app()).get("/v1/models").json()

    assert payload["data"] == [
        {"id": "x/model-a", "object": "model", "owned_by": "opentoken", "capability": TEXT}
    ]


def test_models_endpoint_hides_media_models_without_credentials(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENTOKEN_STATE_DIR", str(tmp_path))

    payload = TestClient(create_app()).get("/v1/models").json()

    ids = {item["id"] for item in payload["data"]}
    assert not {mid for mid in ids if mid.startswith("doubao/seedream")}
    assert not {mid for mid in ids if mid.startswith("doubao/seedance")}
