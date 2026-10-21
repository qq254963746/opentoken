<div align="center">

# OpenToken

**[🇨🇳 中文](README.md) · 🇬🇧 English**

A single local OpenAI-compatible gateway fronting many LLM providers via web sessions and API keys.

</div>

---

A single local **OpenAI-compatible gateway** that fronts many LLM providers — web sessions, browser-harvested cookies, and direct API keys — behind one unified surface. Your downstream client speaks one OpenAI-style dialect; how each provider logs in, what protocol it streams, and how its `<tool_calls>` markup gets normalized is the gateway's problem.

## What it solves

If you use DeepSeek, Qwen, Kimi, Doubao, GLM, Claude, Gemini, ChatGPT, Grok, and Mimo side by side, you've hit it: every provider has a different login, a different model namespace, a different streaming dialect. OpenToken (1) manages credentials in one place, (2) exposes one OpenAI-compatible gateway, (3) maps every provider's models to a uniform call shape, and (4) aligns streaming, tool-calls, and response envelopes to OpenAI's spec.

## Supported providers

**Web sessions / browser-harvested (12)**: DeepSeek · Qwen International · Qwen China · Kimi · Claude · Doubao · ChatGPT · Gemini · Grok · GLM International · GLM China · Xiaomi Mimo

**Doubao** additionally serves browser-driven **image generation** (Seedream) and **video generation** (Seedance) — see "Image / video generation" below.

**Direct API keys (4)**:
- **Manus** (official API)
- **NVIDIA NIM** — free 40 RPM via `integrate.api.nvidia.com/v1`, covering DeepSeek R1 / Llama 3.3 70B / Qwen 2.5 72B & Coder 32B / Mixtral 8x22B and more. Just a NVIDIA account → `nvapi-...` key, no card needed.
- **Unified Proxy (LiteLLM)** — one adapter, 100+ backends through LiteLLM (OpenRouter / Groq / Together / Bedrock / Anthropic / OpenAI / Perplexity / Cohere / Mistral / xAI / Fireworks / DeepInfra / Azure / Ollama / LM Studio …). Soft dependency, install with `uv sync --extra unified`.

## `/v1/models` is fully live-discovered

Each provider discovers its catalog live; any failure soft-degrades to empty. Results cache in `~/.opentoken/model-catalog-cache.json` (6h TTL); cold-start runs every discoverer concurrently under a 45s deadline so one slow provider can't hold up the rest.

| Provider | Discovery |
|----------|-----------|
| qwen-intl / qwen-cn / doubao / glm-cn | web page / dialog scrape (some via Camoufox browser) |
| glm-intl | `GET chat.z.ai/api/models`, fallback to homepage scrape |
| deepseek | `GET /api/v0/users/current` validates, returns the two wire models the protocol supports |
| kimi | scrape model metadata embedded in kimi.com homepage |
| nim | `GET integrate.api.nvidia.com/v1/models` (Bearer auth) |
| manus | `GET api.manus.im/api/v1/agents` |
| chatgpt | `GET /backend-api/models`, fallback to homepage |
| claude | `GET /api/organizations` + statsig chat-models config |
| gemini | scrape gemini.google.com app HTML |
| grok | scrape grok.com homepage |
| mimo | scrape xiaomimo.com homepage |
| unified | filter `litellm.model_cost` by the credentials' configured backends |

**Fallback floor**: qwen-intl's catalog is now JS-rendered; kimi's lives behind a gRPC-Connect endpoint — neither is scrape-able with httpx. When a logged-in provider's live discovery yields nothing, opentoken falls back to a minimal known-wire list (qwen-intl → `qwen3.6-plus`, `qwen-max-latest`; kimi → `k2`, `k1`) so chat still works and the provider doesn't silently vanish from `/v1/models`. Live discovery wins whenever it returns something.

## Requirements

Python **>= 3.13**, [`uv`](https://docs.astral.sh/uv/) recommended.

## On-disk layout

Credentials never enter the repo — everything lives under `~/.opentoken/`:

```
~/.opentoken/
├── config.json              # local gateway config (API key / host / port)
├── providers/<name>.json    # per-provider credentials
├── auth-profiles.json       # cross-provider auth profiles
├── provider-sessions.json   # session state (conversation_id ...; capped 256 LRU)
├── responses.json           # /v1/responses history (7d TTL, 1024 LRU)
├── files/, uploads/         # /v1/files & /v1/uploads binary blobs
├── logs/gateway.log         # rotating log (10 MiB x 5, see "Logs & debugging")
└── model-catalog-cache.json # discovery cache
```

All credential/cookie/token/uploaded blobs are 0600 (owner-only); the directory tree is 0700 (not listable). Every JSON store uses atomic write (tmp + os.replace + flock) with sensitive=True chmod. On a shared host nobody else can read your sessions, conversation history, or uploaded files.

## Quick start

```bash
uv sync
uv run opentoken onboard      # scaffolds ~/.opentoken/
uv run opentoken start        # binds http://127.0.0.1:32117
```

Default base URL: `http://127.0.0.1:32117/v1`.

**Non-loopback binding warning**: `opentoken start --host 0.0.0.0` prints a stderr warning — that exposes every logged-in provider session beyond this machine. With no API key configured, the warning escalates to **UNAUTHENTICATED**.

## Local API key

OpenToken expects the **local gateway key** on inbound requests, not any upstream provider key.

```bash
cat ~/.opentoken/config.json
# {"api_key":"...","host":"127.0.0.1","port":32117}
```

- `config.json` **absent** (pre-first-run): keyless dev path.
- `config.json` exists but `api_key` is empty/whitespace: **503 fail-closed** (usually a rotation in progress, not an intent to disable auth).
- To genuinely run keyless, opt in explicitly with `"keyless_local": true`.
- `config.json` corrupt/unreadable (parse failure or permission denied): also **503 fail-closed**.

## Provider login

Single command: `uv run opentoken login <provider>`.

### A) Browser-based

Launches a real (Camoufox) Firefox, you log in, the cookies/headers land in `~/.opentoken/providers/<provider>.json`:

```bash
uv run opentoken login qwen international --browser
uv run opentoken login qwen china         --browser
uv run opentoken login deepseek           --browser
uv run opentoken login kimi               --browser
uv run opentoken login doubao             --browser
uv run opentoken login glm international  --browser
uv run opentoken login glm china          --browser
uv run opentoken login claude             --browser
uv run opentoken login chatgpt            --browser
uv run opentoken login gemini             --browser
uv run opentoken login grok               --browser
uv run opentoken login mimo               --browser
```

Login is dry-run validated: if existing credentials still work, freshly captured ones must pass an authenticated probe before they replace the working pair. First-time login skips the probe. Browser captures also pass a basic sanity check — at least one of cookie / bearer / access_token / Authorization header must be non-empty, or the record is refused.

> Note: some browser logins (e.g. Kimi) capture credentials only when you **manually close the browser window** after logging in; the CLI prints a prompt to that effect.

### B) Manual cookie / header

```bash
uv run opentoken login qwen international \
  --cookie 'your_cookie_here' \
  --user-agent 'your user agent'

uv run opentoken login deepseek --header 'authorization=Bearer xxx'
```

### C) Direct API key

```bash
uv run opentoken login manus --api-key YOUR_KEY
uv run opentoken login nim   --api-key nvapi-XXXXXXXXXXXXXXXXXXXX
```

NIM credentials accept an optional `model_chain` for cross-model 429 fallback — a rate-limited model transparently switches to the next id in the list:

```json
{
  "kind": "api_key",
  "metadata": {
    "api_key": "nvapi-XXXXXXXXXXXXXXXXXXXX",
    "model_chain": "[\"deepseek-ai/deepseek-r1\", \"meta/llama-3.3-70b-instruct\", \"qwen/qwen2.5-72b-instruct\"]"
  },
  "status": "valid"
}
```

### D) Unified Proxy (LiteLLM)

```bash
uv sync --extra unified

uv run opentoken login unified \
  --header api_key_openrouter=sk-or-XXXXXXXXX \
  --header api_key_anthropic=sk-ant-XXXXXXXX \
  --header api_key_groq=gsk_XXXXXXXX
```

Then call with `unified/<backend>/<model>`: `unified/openrouter/anthropic/claude-3.5-sonnet`, `unified/groq/llama-3.3-70b-versatile`, etc.

**unified streaming + tool_calls is unsupported**: the streaming interface yields plain strings and can't carry structured tool_call deltas; if a backend emits tool_calls mid-stream opentoken raises a clear "retry with stream=false" error rather than silently dropping them.

### Listing & logout

```bash
uv run opentoken providers
uv run opentoken logout qwen international
```

## Verifying the gateway

```bash
curl http://127.0.0.1:32117/health     # → {"status":"ok"}
uv run opentoken status                # quick service status
uv run opentoken doctor                # system diagnostics
uv run opentoken verify                # contract checks (per-provider thread; one slow one doesn't block the rest)
```

Cross-provider E2E smoke (in a second terminal):

```bash
uv run python scripts/live_provider_smoke.py
```

Runs non-stream + stream + `/v1/responses` against every logged-in provider; per-provider pass/fail + first-byte latency lands in `live_provider_smoke_report.json`.

## OpenAI-compatible API

```bash
curl http://127.0.0.1:32117/v1/models -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY'
```

`/v1/models` advertises a single **bare `<provider>/<model>`** format: `qwen-intl/qwen3.7-plus`, `deepseek/deepseek-chat`, `nim/deepseek-ai/deepseek-r1`, `unified/openrouter/anthropic/claude-3.5-sonnet`. The provider segment disambiguates collisions (`glm-cn/glm-5` vs `glm-intl/glm-5`). Alias resolution is **case-insensitive**. (The legacy `algae/<provider>/<model>` namespace form is still accepted by the resolver for backward compatibility, but is no longer listed in `/v1/models`.)

Every entry also carries a `capability` field that says which endpoint to call:

```json
{
  "object": "list",
  "data": [
    {"id": "qwen-intl/qwen3.7-plus", "object": "model", "owned_by": "opentoken", "capability": "text"},
    {"id": "doubao/seedream-5.0-flash", "object": "model", "owned_by": "opentoken", "capability": "image"},
    {"id": "doubao/seedance-2.0-mini", "object": "model", "owned_by": "opentoken", "capability": "video"}
  ]
}
```

| `capability` | Endpoint |
| --- | --- |
| `text` | `/v1/chat/completions`, `/v1/responses` |
| `image` | `/v1/images/generations` |
| `video` | `/v1/videos` |
| `audio` | Reserved — no audio model yet (Doubao's web skill doesn't accept audio) |

**Chat Completions (streaming)**:

```bash
curl http://127.0.0.1:32117/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' -N \
  -d '{
    "model": "qwen-intl/qwen3.7-plus",
    "stream": true,
    "messages": [{"role": "user", "content": "Tell me about yourself"}]
  }'
```

Honored optional params: `temperature` · `max_tokens` · `top_p` · `tools` · `tool_choice`.

SSE conventions: chunks match OpenAI exactly (`object=chat.completion.chunk`, first chunk's `delta.role=assistant`, content chunks carry `delta.content`, a consistent `id` across chunks, a final `finish_reason`, and a `data: [DONE]` terminator); a provider's `<tool_calls>` protocol block at end-of-stream is re-emitted as a standard OpenAI `tool_calls` delta with `finish_reason="tool_calls"`.

`<think>` tags: for reasoner models the streaming path **keeps** `<think>...</think>` markup (clients can show live reasoning), while the non-stream path **strips** it. This is an intentional, test-pinned divergence.

**Responses API**:

```bash
curl http://127.0.0.1:32117/v1/responses \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen-intl/qwen3.7-plus","input":"summarize this"}'
```

When continuing via `previous_response_id`, the new request's `instructions` are **hoisted to the front** of the model context — otherwise the active system prompt would land after the entire prior conversation and be largely ignored. `max_output_tokens` maps onto the unified `max_tokens` field. **`<think>` content is stripped before history is saved** — `previous_response_id` continuations don't feed the model its own scratch reasoning back (cost would double and the model gets biased by its own draft).

**File upload**:

```bash
# single-shot
curl -X POST http://127.0.0.1:32117/v1/files \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -F 'purpose=assistants' -F 'file=@./report.pdf'

# multipart
curl -X POST http://127.0.0.1:32117/v1/uploads \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"filename":"big.bin","bytes":52428800,"purpose":"assistants","mime_type":"application/octet-stream"}'
```

- Each part capped at 100 MiB; declared total `bytes` of an upload also capped at 100 MiB (complete concatenates all parts in memory).
- Each incoming part is rejected with 413 if `existing_parts_bytes + new_part > declared bytes`.
- **`GET /v1/files/{id}/content` always returns `application/octet-stream` + `nosniff` + `attachment`** — never the caller-supplied mime_type, so an uploaded text/html or SVG blob can't execute as stored XSS in a browser-reachable deployment.

**Image generation (Doubao Seedream)**:

```bash
curl http://127.0.0.1:32117/v1/images/generations \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "doubao/seedream-5.0-flash",
    "prompt": "a cat wearing sunglasses, flat illustration style",
    "n": 2,
    "response_format": "url"
  }'
```

- Drives Doubao's web "图像生成" skill through the logged-in Camoufox session — there is no image API upstream, so `opentoken login doubao` is required first.
- Models: `doubao/seedream-5.0-flash` (default) · `doubao/seedream-5.0-pro` · `doubao/seedream-4.5` · `doubao/seedream-4.0`. They appear in `/v1/models` only once Doubao credentials exist.
- `n` is capped at **4**: the web UI renders one image per turn, so `n` runs N serial turns (~20–40s each) rather than a native batch.
- `response_format=url` returns the signed CDN link directly (Doubao signs it out to 2036, so no gateway-side cache); `b64_json` downloads and inlines base64 for clients that refuse external URLs.
- Response shape is OpenAI's: `{"created", "model", "data": [{"url"|"b64_json", "revised_prompt"?}]}`.
- `timeout` bounds the whole request (default 240s, max 600s); a timeout returns **504**.

**Video generation (Doubao Seedance)**:

```bash
# asynchronous (default): returns a job immediately
curl http://127.0.0.1:32117/v1/videos/generations \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"doubao/seedance-2.0-mini","prompt":"a kitten running across a meadow"}'

curl http://127.0.0.1:32117/v1/videos/generations/vidgen-xxxx \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY'
```

```bash
# synchronous: block until the render finishes
curl http://127.0.0.1:32117/v1/videos/generations \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"doubao/seedance-2.0-mini","prompt":"a kitten running across a meadow","wait":true}'
```

- Models: `doubao/seedance-2.0-mini` (default) · `doubao/seedance-2.0-fast` · `doubao/seedance-2.0` · `doubao/seedance-2.5`.
- Full request/response fields, state machine and error table: **[docs/media-generation-api.md](docs/media-generation-api.md)** (Chinese).
- The async POST returns `{"id", "object":"video.generation", "status":"in_progress", "status_url", ...}`; polling yields `status=completed` with `url` / `width` / `height` / `duration_seconds`, or `status=failed` with `error`.
- Delivery is genuinely asynchronous upstream: Doubao first acknowledges with an "预计等待 N 分钟" turn and only posts the video card minutes later. The gateway clicks the player open to read the signed MP4 out of the `<video src>`. `timeout` defaults to 600s (max 900s); a timeout also returns **504**.
- Jobs live in process memory only (1h TTL, pruned past 64 entries). A gateway restart forgets them — the video still exists in the Doubao conversation.

**`/v1/embeddings` currently returns 501**: the previous implementation produced SHA-256-derived pseudo-vectors that would have polluted any real RAG/vector store. Route embedding traffic to a real backend instead. `/v1/models` also no longer advertises `text-embedding-*` so SDK auto-discovery doesn't pick them up.

## Error classification

Both routes share a single classifier (a mid-stream failure goes through the same scrubbing, so an upstream URL / session id never leaks into the SSE error event):

| Upstream error | Gateway returns |
|----------------|-----------------|
| Missing/invalid credentials, session expired, re-login hint, `unauthenticated` (Kimi gRPC), `no chat id` (Qwen) | **401** `authentication_error` |
| Upstream 429 | **429** `rate_limit_error` |
| Upstream 5xx / parse failure / gateway-side exception | **502** `api_error` (no upstream URL leaked) |
| `Unsupported model` / `No route configured` | **400** `invalid_request_error` |

A bare `expired` substring no longer triggers 401 — only `session/credentials/token expired` and similar auth phrases do; "upstream certificate expired" and the like correctly stay 502.

## Bridge to OpenClaw config

```bash
uv run opentoken config --dry-run
uv run opentoken config
uv run opentoken config --opentoken-config /path/to/openclaw-config.json
```

Atomic write (tmp + os.replace) + chmod 0600 — the patch contains the gateway apiKey.

## Logs & debugging

`opentoken start` logs to the terminal **and** to a rotating file (`~/.opentoken/logs/gateway.log`, 10 MiB × 5 by default):

```bash
uv run opentoken start                     # INFO by default, writes ~/.opentoken/logs/gateway.log
uv run opentoken start --log-level debug   # adds composer/click-strategy detail
uv run opentoken start --log-file none     # terminal only
tail -f ~/.opentoken/logs/gateway.log
```

Every line carries a `[request_id]` that matches the `X-Request-Id` response header (a caller-supplied `X-Request-Id` is reused), so one slow request can be followed end to end:

```
INFO  [3677952b…] opentoken.api: request_complete method=POST path=/v1/chat/completions status=200 elapsed_ms=7.98 streaming=true
INFO  [3677952b…] opentoken.gateway.router: route_stream model=doubao/doubao-seed-2.0 provider=doubao adapter=BrowserChatAdapter
INFO  [3677952b…] opentoken.providers.browser: doubao_stream_start model=doubao-seed-2.0 path=dom-first message_chars=18
DEBUG [3677952b…] opentoken.providers.browser: doubao_composer_filled strategy=keystrokes
DEBUG [3677952b…] opentoken.providers.browser: doubao_send_click path=js
INFO  [3677952b…] opentoken.providers.browser: doubao_dom_first_token first_token_ms=6758
INFO  [3677952b…] opentoken.providers.browser: doubao_dom_stream_finished emitted_chars=5 total_ms=7407
INFO  [3677952b…] opentoken.api.chat: stream_complete finish_reason=stop content_chars=5 elapsed_ms=19466
```

For an SSE response, `request_complete` only means the **headers** went out (`streaming=true` → `elapsed_ms` is not the total); the real total is `stream_complete`. Fields worth watching:

| Field | Meaning |
| --- | --- |
| `elapsed_ms` / `total_ms` | per-stage duration |
| `first_token_ms` | time to first token (browser session/page setup + upstream thinking) |
| `strategy` | how the composer was filled; `fill-fallback` means newlines got flattened — investigate |
| `doubao_submit_missing` | the send click produced no `/chat/completion` POST; auto-reloads and retries once |
| `doubao_stream_path_failed` | one path failed and fell through to the other (with `detail`) |

Environment: `OPENTOKEN_LOG_LEVEL`, `OPENTOKEN_LOG_FILE` (set to `none` to disable the file).

## Troubleshooting

- **Empty `/v1/models`** → check `/health` and `opentoken providers`. A provider without valid credentials simply doesn't appear.
- **Always 401** → make sure the `Authorization: Bearer` is the **local gateway key** (`cat ~/.opentoken/config.json`).
- **Flaky streams** → first isolate with `curl -N`. Local gateway issues fail fast/timeout, upstream throttles surface as explicit rate-limit/error events.
- **`NS_ERROR_PROXY_CONNECTION_REFUSED` on browser providers** → the Camoufox-backed providers read the **OS-level** proxy config (independent of `HTTP_PROXY`); an unreachable system proxy surfaces this error. Pure-HTTP providers aren't affected.
- **A provider stuck on 429 / a verify challenge (e.g. Doubao)** → upstream anti-bot against headless automation; retry later or from a different egress IP. The gateway fails fast with 429 rather than hanging.
- **Change bind address** → `opentoken start --host 0.0.0.0 --port 32117` prints a LAN-exposure warning.

## Development & tests

```bash
./.venv/bin/pytest                      # full suite (currently 669+ tests)
./.venv/bin/pytest -k stream            # streaming tests
./.venv/bin/pytest tests/providers/     # provider unit tests
./.venv/bin/pytest tests/storage/       # atomicity + permission tests
```

`tests/` holds the offline unit suite (collected by `pytest`); `scripts/` holds the **live & stress scripts** that need real credentials/a running gateway (run standalone, not part of the unit suite): `live_provider_smoke.py`, `live_provider_200_suite.py`, `live_stream_regression.py`, `live_<provider>_200_cases.py`, `live_regression.py`, `live_doubao_regression.py`, `live_e2e_full.py`, `live_e2e_report.py`, `stress_test.py`. Their generated `*_report*.md` artifacts are gitignored.

## Git hygiene

`.gitignore` already excludes `.venv/`, `.opentoken/`, `tmp/`, `*.log`. **Never commit** anything from `~/.opentoken/`, exported cookies/headers/tokens, `.env*`, or debug logs containing tokens. Always `git status` first; bail if any credential file is staged.

## License

MIT — see `LICENSE`.
