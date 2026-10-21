from opentoken.models.discovery import (
    _discover_qwen_cn_models,
    _extract_doubao_models_from_html,
    _extract_glm_cn_models_from_html,
    _extract_qwen_cn_models_from_dialog_text,
    _extract_qwen_intl_models_from_html,
    load_model_catalog,
)
from opentoken.models.provider_credentials import ProviderCredentialRecord


def test_discover_qwen_cn_routes_browser_work_through_persistent_worker(monkeypatch) -> None:
    """qwen-cn discovery must run its Playwright work on the persistent per-
    provider worker thread (via _run_browser_completion), not directly on the
    discovery executor thread — otherwise repeated /v1/models calls close the
    browser session cross-thread (Playwright thread-affinity violation)."""
    import json as _json

    captured: dict[str, object] = {}

    def fake_run_browser_completion(*, provider_name, invoke, **kwargs):
        captured["provider_name"] = provider_name
        # Simulate the worker thread invoking the closure and carrying back a str.
        return invoke()

    # The discoverer's _with_page is what would touch Playwright; stub it so the
    # closure returns a known model list without launching a browser.
    monkeypatch.setattr(
        "opentoken.providers.browser._run_browser_completion",
        fake_run_browser_completion,
    )
    monkeypatch.setattr(
        "opentoken.providers.camoufox_clients.CamoufoxProviderClient._with_page",
        lambda self, *, start_url, cookie_domains, action: [("Qwen3-Max", "Qwen3 Max")],
    )

    creds = ProviderCredentialRecord(
        provider="qwen-cn", kind="browser_session", cookie="x", headers={},
        user_agent="ua", metadata={}, status="valid",
    )
    from pathlib import Path

    result = _discover_qwen_cn_models(creds, Path("/tmp"))
    assert captured["provider_name"] == "qwen-cn"
    assert result == [("Qwen3-Max", "Qwen3 Max")]


def test_extract_qwen_intl_models_from_html_returns_model_entries() -> None:
    html = """
    <script>
    {"id":"qwen3.6-plus","name":"Qwen3.6-Plus","object":"model","owned_by":"qwen"}
    {"id":"qwen3.5-flash","name":"Qwen3.5-Flash","object":"model","owned_by":"qwen"}
    </script>
    """

    assert _extract_qwen_intl_models_from_html(html) == [
        ("qwen3.6-plus", "Qwen3.6-Plus"),
        ("qwen3.5-flash", "Qwen3.5-Flash"),
    ]


def test_extract_qwen_cn_models_from_dialog_text_returns_current_labels() -> None:
    dialog_text = (
        "模型 "
        "Qwen3.5-千问 综合AI助手，全面回答工作、学习、生活各类问题 "
        "Qwen3.5-Flash 适用于简单任务，响应速度快 "
        "Qwen3-Max 适用于日常通用型任务，综合能力均衡 "
        "Qwen3-Max-Thinking 适用于多步骤推理与问题分析 "
        "Qwen3-Coder 代码 适用于代码生成与编程任务执行"
    )

    assert _extract_qwen_cn_models_from_dialog_text(dialog_text) == [
        ("Qwen3.5-千问", "Qwen3.5-千问"),
        ("Qwen3.5-Flash", "Qwen3.5-Flash"),
        ("Qwen3-Max", "Qwen3-Max"),
        ("Qwen3-Max-Thinking", "Qwen3-Max-Thinking"),
        ("Qwen3-Coder", "Qwen3-Coder"),
    ]


def test_extract_doubao_models_from_html_returns_current_action_bar_models() -> None:
    html = """
    <script>
    {"action_bar_menu_config":{"menu_item_list":[
      {"menu_type":0,"name":"快速","sub_title_name":"适用于大部分情况"},
      {"menu_type":1,"name":"思考","sub_title_name":"擅长解决更难的问题"},
      {"menu_type":3,"name":"专家","sub_title_name":"研究级智能模型"}
    ],"default_deep_think_auto":false}}
    </script>
    """

    assert _extract_doubao_models_from_html(html) == [
        ("doubao-seed-2.0", "Doubao 快速"),
        ("doubao-thinking", "Doubao 思考"),
        ("doubao-pro", "Doubao 专家"),
    ]


def test_extract_doubao_models_from_html_reads_current_mode_select_config() -> None:
    """The picker moved to `modeSelectConfig.menu_item_list`; each item nests an
    `option_list` array (reasoning effort), so a non-greedy regex truncates the
    payload. Only chat-capable entries ("工作任务 …" is office-only) belong in
    /v1/models, and labels may carry a "豆包 " prefix."""
    html = """
    <script>
    {"modeSelectConfig":{"menu_item_list":[
      {"item_id":0,"menu_type":0,"model_item_key":"0","name":"快速",
       "conversation_mode_list":["chat","creation"],
       "reasoning_effort_config":{"option_list":[{"level":3,"name":"低"},{"level":4,"name":"中"}]}},
      {"item_id":3,"menu_type":3,"model_item_key":"3","name":"豆包 专家",
       "conversation_mode_list":["chat","creation"],
       "reasoning_effort_config":{"option_list":[{"level":3,"name":"低"}]}},
      {"item_id":9,"menu_type":9,"model_item_key":"9","name":"工作任务 Auto",
       "conversation_mode_list":["office","creation"]},
      {"item_id":10001,"menu_type":10001,"model_item_key":"seed-lite-7b","name":"豆包 2.1 Lite",
       "conversation_mode_list":["office","creation"]}
    ]},"modeSelectData":{"defaultModeSelectValue":"0"}}
    </script>
    """

    assert _extract_doubao_models_from_html(html) == [
        ("doubao-seed-2.0", "Doubao 快速"),
        ("doubao-pro", "Doubao 专家"),
    ]


def test_extract_doubao_models_from_html_returns_empty_when_menu_missing() -> None:
    assert _extract_doubao_models_from_html("<html><body>豆包</body></html>") == []


def test_extract_glm_cn_models_from_html_returns_meta_models() -> None:
    html = """
    <html>
      <head>
        <meta name="keywords" content="GLM-5,大语言模型,多模态AI,AI编程,AI翻译,智谱" />
        <meta name="description" content="GLM-5 的全能 AI 助手，支持精通对话、写作与编程。" />
      </head>
    </html>
    """

    assert _extract_glm_cn_models_from_html(html) == [
        ("glm-5", "GLM-5"),
    ]


def test_load_model_catalog_replaces_fallback_provider_entries_with_dynamic_discovery(
    monkeypatch,
    tmp_path,
) -> None:
    credentials = ProviderCredentialRecord(
        provider="qwen-intl",
        kind="browser_session",
        cookie="session=1",
        headers={},
        user_agent="ua",
        metadata={},
        status="valid",
    )

    monkeypatch.setattr(
        "opentoken.models.discovery.load_provider_credentials",
        lambda providers_dir, provider: credentials if provider == "qwen-intl" else None,
    )
    monkeypatch.setattr(
        "opentoken.models.discovery._DISCOVERERS",
        {
            "qwen-intl": lambda credentials, state_dir: [
                ("qwen3.6-plus", "Qwen3.6-Plus"),
                ("qwen3.5-flash", "Qwen3.5-Flash"),
            ]
        },
    )

    catalog = load_model_catalog(
        state_dir=tmp_path,
        providers_dir=tmp_path / "providers",
        use_cache=False,
    )
    qwen_models = sorted(entry.id for entry in catalog if "/qwen-intl/" in entry.id)

    assert qwen_models == [
        "algae/qwen-intl/qwen3.5-flash",
        "algae/qwen-intl/qwen3.6-plus",
    ]


def test_load_model_catalog_falls_back_for_logged_in_provider_when_discovery_empty(
    monkeypatch,
    tmp_path,
) -> None:
    """A logged-in provider whose live discovery yields nothing must still
    surface its known wire models, so /v1/models lists it and the smoke script
    can test it. This is the floor for JS-rendered (qwen-intl) / gRPC (kimi)
    catalogs we can't scrape — live discovery is still tried first and wins."""
    credentials = ProviderCredentialRecord(
        provider="kimi",
        kind="web_session",
        cookie="session=1",
        headers={},
        user_agent="ua",
        metadata={},
        status="valid",
    )

    monkeypatch.setattr(
        "opentoken.models.discovery.load_provider_credentials",
        lambda providers_dir, provider: credentials if provider == "kimi" else None,
    )
    # Discoverer runs but finds nothing (page shape changed / endpoint moved).
    monkeypatch.setattr(
        "opentoken.models.discovery._DISCOVERERS",
        {"kimi": lambda credentials, state_dir: []},
    )
    monkeypatch.setattr(
        "opentoken.models.discovery._FALLBACK_MODELS",
        {"kimi": [("k2", "Kimi K2"), ("k1", "Kimi K1")]},
    )

    catalog = load_model_catalog(
        state_dir=tmp_path,
        providers_dir=tmp_path / "providers",
        use_cache=False,
    )
    kimi_models = sorted(entry.id for entry in catalog if "/kimi/" in entry.id)

    assert kimi_models == ["algae/kimi/k1", "algae/kimi/k2"]


def test_load_model_catalog_prefers_live_discovery_over_fallback(
    monkeypatch,
    tmp_path,
) -> None:
    """When live discovery succeeds, the fallback floor must not leak in — the
    listed models are exactly what the provider page returned."""
    credentials = ProviderCredentialRecord(
        provider="kimi",
        kind="web_session",
        cookie="session=1",
        headers={},
        user_agent="ua",
        metadata={},
        status="valid",
    )

    monkeypatch.setattr(
        "opentoken.models.discovery.load_provider_credentials",
        lambda providers_dir, provider: credentials if provider == "kimi" else None,
    )
    monkeypatch.setattr(
        "opentoken.models.discovery._DISCOVERERS",
        {"kimi": lambda credentials, state_dir: [("k3", "Kimi K3")]},
    )
    monkeypatch.setattr(
        "opentoken.models.discovery._FALLBACK_MODELS",
        {"kimi": [("k2", "Kimi K2"), ("k1", "Kimi K1")]},
    )

    catalog = load_model_catalog(
        state_dir=tmp_path,
        providers_dir=tmp_path / "providers",
        use_cache=False,
    )
    kimi_models = sorted(entry.id for entry in catalog if "/kimi/" in entry.id)

    assert kimi_models == ["algae/kimi/k3"]


def test_load_cached_models_rejects_expires_at_too_far_in_future(tmp_path) -> None:
    """Wall-clock 反向（NTP / container restore）→ expires_at 看起来还在未来,
    永不失效。如果距 now 大于 TTL,视为已过期清掉,避免缓存被时钟跳跃锁住。"""
    from opentoken.models.discovery import (
        _DISCOVERY_TTL_SECONDS,
        _load_cached_models,
        _store_cached_models,
    )

    creds = ProviderCredentialRecord(
        provider="alpha", kind="web_session", cookie="x", headers={},
        user_agent="ua", metadata={}, status="valid",
    )
    cache: dict = {}
    # 在远未来时间点写入,模拟系统时钟跳到未来后又跳回的场景
    _store_cached_models(cache, provider="alpha", credentials=creds, models=[("m1", "M1")], now=10_000.0)

    # 之后系统时钟回到现在,now 远在 expires_at 之前
    # expires_at = 10000 + 6h(21600) = 31600; now = 0; 距 now = 31600 > TTL → 拒
    assert _load_cached_models(cache, provider="alpha", credentials=creds, now=0.0) is None

    # 正常路径：now 在 expires_at 之前但距它不超过 TTL → 返
    result = _load_cached_models(
        cache, provider="alpha", credentials=creds,
        now=10_000.0 + _DISCOVERY_TTL_SECONDS - 100
    )
    assert result == [("m1", "M1")]


def test_persist_discovered_models_merges_with_concurrent_writers(tmp_path) -> None:
    """Lost-update regression: two /v1/models passes both take an empty top-of-
    function cache snapshot, then each tries to persist its own provider. The
    later writer must NOT clobber the earlier one's entry — the persist helper
    re-reads the cache under an exclusive file lock and merges onto the latest
    on-disk state before writing.

    Simulate the race deterministically: pass A's snapshot was empty; pass B
    has already written {beta} to disk while A was discovering; A now calls
    _persist_discovered_models with only its own {alpha} discoveries. Without
    the merge, the resulting cache would be {alpha}; with it, {alpha, beta}.
    """
    from opentoken.models.discovery import _load_cache, _persist_discovered_models

    creds_alpha = ProviderCredentialRecord(
        provider="alpha", kind="web_session", cookie="x", headers={},
        user_agent="ua", metadata={}, status="valid",
    )
    creds_beta = ProviderCredentialRecord(
        provider="beta", kind="web_session", cookie="x", headers={},
        user_agent="ua", metadata={}, status="valid",
    )

    cache_path = tmp_path / "model-catalog-cache.json"

    # Pass B's write landed first.
    _persist_discovered_models(
        cache_path,
        discovered_results={"beta": [("b1", "B 1")]},
        creds_by_provider={"beta": creds_beta},
        now=1000.0,
    )

    # Pass A's write happens later, but A's in-memory snapshot was empty (the
    # bug). The merge must still preserve beta from disk.
    _persist_discovered_models(
        cache_path,
        discovered_results={"alpha": [("a1", "A 1")]},
        creds_by_provider={"alpha": creds_alpha},
        now=1001.0,
    )

    on_disk = _load_cache(cache_path)
    providers_in_cache = sorted(key.split(":", 1)[0] for key in on_disk)
    assert providers_in_cache == ["alpha", "beta"]


def test_load_model_catalog_runs_discoverers_concurrently_and_isolates_failures(
    monkeypatch,
    tmp_path,
) -> None:
    """The loader runs every logged-in provider's discoverer in parallel under
    an overall wall-clock budget. One discoverer raising must not knock out the
    others; a slow discoverer that beats the deadline still contributes.

    This guards the cold-cache /v1/models path that previously timed out
    because discoverers ran sequentially in the request thread.
    """
    import threading
    import time as time_module

    def _stub_creds(provider: str) -> ProviderCredentialRecord:
        return ProviderCredentialRecord(
            provider=provider,
            kind="web_session",
            cookie="x",
            headers={},
            user_agent="ua",
            metadata={},
            status="valid",
        )

    monkeypatch.setattr(
        "opentoken.models.discovery.load_provider_credentials",
        lambda providers_dir, provider: _stub_creds(provider)
        if provider in {"good_a", "good_b", "boom"}
        else None,
    )

    call_starts: list[tuple[str, float]] = []
    barrier = threading.Event()

    def good_a(_credentials, _state_dir):
        call_starts.append(("good_a", time_module.monotonic()))
        barrier.wait(timeout=2.0)  # Coordinate with good_b to prove parallelism.
        return [("a-1", "A 1"), ("a-2", "A 2")]

    def good_b(_credentials, _state_dir):
        call_starts.append(("good_b", time_module.monotonic()))
        barrier.set()
        return [("b-1", "B 1")]

    def boom(_credentials, _state_dir):
        raise RuntimeError("upstream is down")

    monkeypatch.setattr(
        "opentoken.models.discovery._DISCOVERERS",
        {"good_a": good_a, "good_b": good_b, "boom": boom},
    )

    catalog = load_model_catalog(
        state_dir=tmp_path,
        providers_dir=tmp_path / "providers",
        use_cache=False,
    )
    ids = sorted(entry.id for entry in catalog)

    # Two good providers contributed; the raising one was isolated.
    assert ids == [
        "algae/good_a/a-1",
        "algae/good_a/a-2",
        "algae/good_b/b-1",
    ]
    # Parallelism: the barrier only releases when good_b runs, so good_a's
    # barrier.wait would time out if discoverers were serialised. Both must have
    # started. (boom raises before recording, so it's not in call_starts.)
    started = {name for name, _ in call_starts}
    assert started == {"good_a", "good_b"}


def test_load_model_catalog_returns_at_deadline_despite_hung_discoverer(
    monkeypatch,
    tmp_path,
) -> None:
    """A discoverer that never returns must not hold /v1/models hostage. The
    loader breaks at the wall-clock deadline and shuts the executor down
    without waiting (shutdown(wait=False, cancel_futures=True)); the naive
    `with ThreadPoolExecutor` form would block on shutdown(wait=True) until the
    hung discoverer finished, defeating the deadline.
    """
    import threading
    import time as time_module

    monkeypatch.setattr("opentoken.models.discovery._DISCOVERY_PASS_DEADLINE_SECONDS", 0.5)

    release = threading.Event()

    def _stub_creds(provider: str) -> ProviderCredentialRecord:
        return ProviderCredentialRecord(
            provider=provider, kind="web_session", cookie="x", headers={},
            user_agent="ua", metadata={}, status="valid",
        )

    monkeypatch.setattr(
        "opentoken.models.discovery.load_provider_credentials",
        lambda providers_dir, provider: _stub_creds(provider)
        if provider in {"fast", "hung"}
        else None,
    )

    def fast(_credentials, _state_dir):
        return [("f-1", "F 1")]

    def hung(_credentials, _state_dir):
        # Block well past the deadline; released in finally so the worker thread
        # doesn't outlive the test.
        release.wait(30.0)
        return [("h-1", "H 1")]

    monkeypatch.setattr(
        "opentoken.models.discovery._DISCOVERERS",
        {"fast": fast, "hung": hung},
    )

    try:
        start = time_module.monotonic()
        catalog = load_model_catalog(
            state_dir=tmp_path,
            providers_dir=tmp_path / "providers",
            use_cache=False,
        )
        elapsed = time_module.monotonic() - start

        # Returned promptly (deadline 0.5s + scheduling slack), not after 30s.
        assert elapsed < 5.0, f"load_model_catalog blocked on hung discoverer ({elapsed:.1f}s)"
        # The fast provider contributed; the hung one simply didn't make it.
        ids = sorted(entry.id for entry in catalog)
        assert ids == ["algae/fast/f-1"]
    finally:
        release.set()


def test_filter_user_facing_models_drops_internal_builds() -> None:
    """The /v1/models catalog must show the user-facing lineup, not the raw
    backend registry dump (internal/preview/dated/param-sized builds). This is
    the 'pile of preset junk' the catalog used to leak for qwen-intl/glm-intl."""
    from opentoken.models.discovery import _filter_user_facing_models, _is_internal_model_id

    junk = [
        "qwen-latest-series-invite-beta-v24",
        "qwen3.5-397b-a17b",
        "qwen3.5-122b-a10b",
        "qwen3.6-35b-a3b",
        "qwen3.5-27b",
        "qwen3.5-max-2026-03-08",
        "qwen3-max-2026-01-23",
        "qwen3.6-plus-preview",
        "0727-106B-API",
        "0808-360B-DR",
        "glm-4-air-250414",
    ]
    real = [
        "qwen3.7-plus",
        "qwen3.7-max",
        "qwen3-coder-plus",
        "qwen3-vl-plus",
        "GLM-5.1",
        "glm-5",
        "glm-4.7",
        "deepseek-chat",
        "deepseek-reasoner",
        "doubao-pro",
        "Qwen3-Max",
        "k2",
    ]
    for mid in junk:
        assert _is_internal_model_id(mid), f"should drop internal build: {mid}"
    for mid in real:
        assert not _is_internal_model_id(mid), f"should keep real model: {mid}"

    mixed = [(m, m) for m in junk + real]
    kept = {mid for mid, _ in _filter_user_facing_models(mixed)}
    assert kept == set(real)
    assert not (kept & set(junk))
