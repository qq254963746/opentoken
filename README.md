<div align="center">

# OpenToken

**🇨🇳 中文 · [🇬🇧 English](README.en.md)**

把多家 LLM 网页登录态 / API key 凭证统一成一个本地 OpenAI 兼容网关。

</div>

---

把 DeepSeek / Qwen / Kimi / Doubao / GLM / Claude / Gemini / ChatGPT / Grok / Mimo / NVIDIA NIM / Manus / Unified (LiteLLM) 等多家 provider 的网页登录态、cookies、API key 统一封装成一个**本地 OpenAI 兼容网关**。你的下游客户端只对一个 OpenAI 风格端点说话；上游怎么登录、走什么协议、流式怎么剥 `<tool_calls>` 标签都被网关吃掉。

## 它解决什么

- 每家 provider 登录方式 / 模型名 / 流式协议 / 错误码都不一样
- OpenAI 风格的客户端（你的脚本 / IDE 插件 / 第三方 app）只想说一种话
- 凭证文件零散且容易意外提交进 Git

OpenToken 做的：(1) 凭证统一管理；(2) 本地 OpenAI 兼容 HTTP 接口；(3) 模型名映射；(4) 流式 / tool_calls / 错误响应对齐到 OpenAI spec。

## 支持的 Provider

**网页登录态 / 浏览器采集（12 个）**：DeepSeek · Qwen International · Qwen China · Kimi · Claude · Doubao · ChatGPT · Gemini · Grok · GLM International · GLM China · Xiaomi Mimo

其中 **Doubao** 额外提供网页驱动的**图片生成**（Seedream）与**视频生成**（Seedance），见下文「图像生成 / 视频生成」。

**API key 直连（4 个）**：
- **Manus**（官方 API）
- **NVIDIA NIM** — `integrate.api.nvidia.com/v1`，免费 40 RPM。覆盖 DeepSeek R1 / Llama 3.3 70B / Qwen 2.5 72B / Mixtral 8x22B 等。注册 NVIDIA 账号拿 `nvapi-...` key 就能用，0 信用卡。
- **Unified Proxy (LiteLLM)** — 一个 adapter 接 100+ 后端（OpenRouter / Groq / Together / Bedrock / Anthropic / OpenAI / Perplexity / Cohere / Mistral / xAI / Fireworks / DeepInfra / Azure / Ollama / LM Studio …）。软依赖，`uv sync --extra unified` 装。

## /v1/models 全部实时发现

每家 provider 各自的发现路径见下表；任何一家失败软降级为空，结果缓存在 `~/.opentoken/model-catalog-cache.json`（TTL 6h）。冷启动并发跑所有发现器并尊重 45s 全局 deadline。

| Provider | 发现方式 |
|----------|---------|
| qwen-intl / qwen-cn / doubao / glm-cn | web 页面 / dialog 抓取（部分走 Camoufox 浏览器） |
| glm-intl | `GET chat.z.ai/api/models`，失败回退抓首页 |
| deepseek | `GET /api/v0/users/current` 校验后返协议支持的两个 wire 模型 |
| kimi | 抓 kimi.com 首页嵌入的 model metadata |
| nim | `GET integrate.api.nvidia.com/v1/models` Bearer auth |
| manus | `GET api.manus.im/api/v1/agents` |
| chatgpt | `GET /backend-api/models`，失败回退首页 |
| claude | `GET /api/organizations` + statsig chat-models 配置 |
| gemini | 抓 gemini.google.com app HTML |
| grok | 抓 grok.com 首页 HTML |
| mimo | 抓 xiaomimo.com 首页 HTML |
| unified | 按凭证里配置的 backend 过滤 `litellm.model_cost` |

**Fallback 楼板**：qwen-intl 的目录现在是纯 JS 渲染，kimi 的目录在 gRPC-Connect 后面，httpx 抓不到。已登录但实时发现返回空时，opentoken 用最小已知 wire 模型清单填底（qwen-intl → `qwen3.6-plus` / `qwen-max-latest`；kimi → `k2` / `k1`），让 chat 仍然可用、provider 不会从 `/v1/models` 静默消失。实时发现一旦恢复就直接覆盖楼板。

## 环境要求

Python **>= 3.13**，推荐用 [`uv`](https://docs.astral.sh/uv/)。

## 目录与本地状态

凭证不进仓库，全部写到 `~/.opentoken/`：

```
~/.opentoken/
├── config.json              # 本地网关配置（含本地 API key / host / port）
├── providers/<name>.json    # 各 provider 凭证
├── auth-profiles.json       # 跨 provider 的认证 profile
├── provider-sessions.json   # 会话上下文（conversation_id 等，capped 256 entries LRU）
├── responses.json           # /v1/responses 历史（TTL 7d, 1024 entries LRU）
├── files/, uploads/         # /v1/files & /v1/uploads 二进制内容
├── logs/gateway.log         # 轮转日志（10 MiB × 5，字段见「日志与排查」）
└── model-catalog-cache.json # 模型发现缓存
```

所有凭证 / cookie / token / 上传内容文件 0600（owner-only），目录树 0700（不可列）。所有 JSON 持久化走原子写（tmp + os.replace + flock）+ sensitive=True 强制 chmod。多用户主机上别人既看不到你的 cookie 也看不到对话历史和上传文件。

## 快速开始

```bash
uv sync
uv run opentoken onboard      # 初始化 ~/.opentoken/
uv run opentoken start        # 默认监听 http://127.0.0.1:32117
```

默认 base URL：`http://127.0.0.1:32117/v1`。

**非 loopback 绑定的警告**：`opentoken start --host 0.0.0.0` 会在 stderr 打印警告，因为这会把你已登录的 provider 会话暴露到本机之外；同时没配 API key 时警告升级为 **UNAUTHENTICATED**。

## 本地 API key

OpenToken 用的是**本地网关自己的 API key**（不是上游 provider 的 key）。

```bash
cat ~/.opentoken/config.json
# {"api_key":"...","host":"127.0.0.1","port":32117}
```

- 配置文件**不存在**（首次启动前）：视为开发场景 keyless 放行
- 配置文件存在但 `api_key` 是空字符串 / 纯空白：**fail closed 503**（rotation 中清空 key 忘了换是常见误操作，不能默默放行）
- 真要 keyless 本地模式：显式 `"keyless_local": true` opt-in
- `config.json` 截断 / 不是合法 JSON / 不可读：同样 **fail closed 503**

## Provider 登录

统一命令：`uv run opentoken login <provider>`。

### 方式 A：浏览器登录

打开真实 Firefox（Camoufox）让你登录，凭证保存到 `~/.opentoken/providers/<provider>.json`：

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

登录有 dry-run 校验：旧凭证仍有效时，新捕获到的必须通过认证 probe 才能覆盖，避免半成 harvest 把可用 cookie 替换成坏的。首次登录跳过 probe。浏览器登录后还会做 basic sanity check：至少要有 cookie / bearer / access_token / Authorization header 中一项非空，否则直接拒收。

> 提示：部分浏览器登录（如 Kimi）需要你在登录完成后**手动关闭浏览器窗口**来触发凭证捕获；命令行里会打印提示。

### 方式 B：手工凭证

```bash
uv run opentoken login qwen international \
  --cookie 'your_cookie_here' \
  --user-agent 'your user agent'

uv run opentoken login deepseek --header 'authorization=Bearer xxx'
```

### 方式 C：API key

```bash
uv run opentoken login manus --api-key YOUR_KEY
uv run opentoken login nim   --api-key nvapi-XXXXXXXXXXXXXXXXXXXX
```

NIM 凭证可选 `model_chain` 跨模型 fallback —— 被 429 的模型自动切到链表里下一个，调用方无感：

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

### 方式 D：Unified Proxy (LiteLLM)

```bash
uv sync --extra unified

uv run opentoken login unified \
  --header api_key_openrouter=sk-or-XXXXXXXXX \
  --header api_key_anthropic=sk-ant-XXXXXXXX \
  --header api_key_groq=gsk_XXXXXXXX
```

调用模型形如：`unified/openrouter/anthropic/claude-3.5-sonnet`、`unified/groq/llama-3.3-70b-versatile`、`unified/together/qwen/qwen2.5-coder-32b-instruct`。

**unified 流式 + tool_calls 暂不支持**：流式接口用 `Iterator[str]` 无法承载结构化 tool_call delta；backend 在流里发出 tool_calls 时 opentoken 显式抛错让你 `stream=false` 重试，而不是静默吞掉。

### 查看状态 / 登出

```bash
uv run opentoken providers
uv run opentoken logout qwen international
```

## 启动后验证

```bash
curl http://127.0.0.1:32117/health     # → {"status":"ok"}
uv run opentoken status                # 服务状态
uv run opentoken doctor                # 系统诊断
uv run opentoken verify                # 接口契约验证（每 provider 独立线程，单个慢的不阻塞整轮）
```

跨 provider E2E 烟雾测试（另起终端）：

```bash
uv run python scripts/live_provider_smoke.py
```

每个已登录 provider 跑一次非流 + 一次流 + 一次 `/v1/responses`，写入 `live_provider_smoke_report.json`。

## OpenAI 兼容调用

```bash
curl http://127.0.0.1:32117/v1/models -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY'
```

`/v1/models` 只列**裸名 `<provider>/<model>`** 一种格式：`qwen-intl/qwen3.7-plus`、`deepseek/deepseek-chat`、`nim/deepseek-ai/deepseek-r1`、`unified/openrouter/anthropic/claude-3.5-sonnet`。provider 段用于消歧（`glm-cn/glm-5` 与 `glm-intl/glm-5` 区分）。模型别名**大小写不敏感**。（旧的 `algae/<provider>/<model>` 命名空间形式仍被解析器接受,向后兼容,但不再列在 `/v1/models`。）

每个条目多带一个 `capability` 字段，告诉客户端该往哪个端点发：

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

| `capability` | 端点 |
| --- | --- |
| `text` | `/v1/chat/completions`、`/v1/responses` |
| `image` | `/v1/images/generations` |
| `video` | `/v1/videos` |
| `audio` | 预留，暂无模型（豆包 Web skill 不支持音频） |

**Chat Completions（流式）**：

```bash
curl http://127.0.0.1:32117/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' -N \
  -d '{
    "model": "qwen-intl/qwen3.7-plus",
    "stream": true,
    "messages": [{"role": "user", "content": "来一段3000字自我介绍"}]
  }'
```

支持的可选参数：`temperature` · `max_tokens` · `top_p` · `tools` · `tool_choice`。

SSE 协议约定：流式 chunk 完全对齐 OpenAI（`object=chat.completion.chunk`、首帧 `delta.role=assistant`、内容帧 `delta.content`、跨帧 `id` 一致、末帧 `finish_reason`、`data: [DONE]` 终止符）；provider 输出的 `<tool_calls>` 协议块在流末尾被重组为标准 OpenAI `tool_calls` delta，`finish_reason` 设为 `tool_calls`。

`<think>` 标签：推理模型（含 `reasoner` / `thinking` / `-think` 关键字）流式里**保留** `<think>...</think>` 让客户端实时看到推理过程；非流式响应**剥离**只留最终答案。这是有意的、测试钉住的行为。

**Responses API**：

```bash
curl http://127.0.0.1:32117/v1/responses \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen-intl/qwen3.7-plus","input":"你好，写一个摘要"}'
```

带 `previous_response_id` 续聊时，新请求里的 `instructions` 会被**置于上下文最前**而不是拼到历史尾部。`max_output_tokens` 自动映射到 `max_tokens`。**保存历史时 `<think>` 内容会被剥掉**——续聊不会把模型自己的推理草稿喂回去（成本翻倍 + 模型被自己干扰）。

**文件上传**：

```bash
# 一次性上传
curl -X POST http://127.0.0.1:32117/v1/files \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -F 'purpose=assistants' -F 'file=@./report.pdf'

# 分块上传
curl -X POST http://127.0.0.1:32117/v1/uploads \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"filename":"big.bin","bytes":52428800,"purpose":"assistants","mime_type":"application/octet-stream"}'
```

- 每 part 100 MiB cap，整个 upload 声明 `bytes` ≤ 100 MiB（complete 会把所有 part 拼到内存里）
- 每个 part 进来时校验"已有 parts 字节 + 本 part > 声明 bytes" → 413 拒
- **`GET /v1/files/{id}/content` 始终回 `application/octet-stream` + `nosniff` + `attachment`**，不回显上传时声明的 mime_type，防止 stored XSS

**图像生成（Doubao Seedream）**：

```bash
curl http://127.0.0.1:32117/v1/images/generations \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "doubao/seedream-5.0-flash",
    "prompt": "一只戴墨镜的猫，扁平插画风格",
    "n": 2,
    "response_format": "url"
  }'
```

- 走豆包网页的「图像生成」技能（Seedream），用已登录的 Camoufox 会话驱动；上游没有图片 API，所以**必须**先 `opentoken login doubao`。
- 可选模型：`doubao/seedream-5.0-flash`（默认）· `doubao/seedream-5.0-pro` · `doubao/seedream-4.5` · `doubao/seedream-4.0`；这四款登录后才会出现在 `/v1/models`。
- `n` 上限 **4**：网页一次只渲染一张，`n` 是串行跑多轮（每张约 20–40s），不是一次批量出图。
- `response_format=url` 直接回签名 CDN 链接（豆包侧有效期到 2036，无需网关缓存）；`response_format=b64_json` 由网关下载后内联 base64，适合不接受外链的客户端。
- 响应是 OpenAI 形状：`{"created", "model", "data": [{"url"|"b64_json", "revised_prompt"?}]}`。
- 单次整体预算由 `timeout` 控制（默认 240s，上限 600s），超时回 **504**。

**视频生成（Doubao Seedance）**：

```bash
# 异步（默认）：立即返回 job，稍后轮询
curl http://127.0.0.1:32117/v1/videos/generations \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"doubao/seedance-2.0-mini","prompt":"一只小猫在草地上奔跑"}'

curl http://127.0.0.1:32117/v1/videos/generations/vidgen-xxxx \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY'
```

```bash
# 同步：等渲染完成直接拿结果（豆包自己提示"预计等待 5 分钟"）
curl http://127.0.0.1:32117/v1/videos/generations \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"doubao/seedance-2.0-mini","prompt":"一只小猫在草地上奔跑","wait":true}'
```

- 可选模型：`doubao/seedance-2.0-mini`（默认）· `doubao/seedance-2.0-fast` · `doubao/seedance-2.0` · `doubao/seedance-2.5`。
- 完整的请求/响应字段、状态机、错误码见 **[docs/media-generation-api.md](docs/media-generation-api.md)**。
- 异步返回 `{"id", "object":"video.generation", "status":"in_progress", "status_url", ...}`；轮询到 `status=completed` 时带 `url` / `width` / `height` / `duration_seconds`，失败时带 `error`。
- 视频是豆包**异步**投递的：先回一条"预计等待 N 分钟"的确认，几分钟后才发视频卡片；网关会点开播放器拿 `<video src>` 里的签名 MP4 直链。`timeout` 默认 600s，上限 900s，超时同样回 **504**。
- job 只存在网关进程内存里（TTL 1h，超过 64 条时按 TTL 清理）；网关重启后旧 job 查不到，但视频仍在豆包会话里。

**`/v1/embeddings` 当前 501**：早期是 SHA-256 派生的伪向量，会污染 RAG 数据源 → 直接拒，把流量路由到真实 backend。`/v1/models` 也不再列 `text-embedding-*` 让 SDK auto-discover 别拿了再调。

## 错误分类

`/v1/chat/completions` 和 `/v1/responses` 共用同一个分类器（流式中途出错也走同一脱敏逻辑，绝不把上游 URL / session 泄漏进 SSE error 事件）：

| 上游错误 | 网关返回 |
|---------|---------|
| 缺失 / 失效凭证、session 过期、re-login 提示、`unauthenticated`（Kimi gRPC）、`no chat id`（Qwen） | **401** `authentication_error` |
| 上游 429 | **429** `rate_limit_error` |
| 上游 5xx / 解析失败 / 网关侧异常 | **502** `api_error`（不暴露上游 URL） |
| `Unsupported model` / `No route configured` | **400** `invalid_request_error` |

bare `expired` 字符串不再单独触发 401 —— 必须配合 `session/credentials/token expired` 等明确的 auth subject 才映射为 401，避免 "upstream certificate expired" 等被误判。

## 注入到外部 OpenClaw 配置

```bash
uv run opentoken config --dry-run
uv run opentoken config
uv run opentoken config --opentoken-config /path/to/openclaw-config.json
```

原子写（tmp + os.replace）+ chmod 0600（patch 含本地 apiKey）。

## 日志与排查

`opentoken start` 会把日志同时打到终端和轮转文件（默认 `~/.opentoken/logs/gateway.log`，10 MiB × 5 份）：

```bash
uv run opentoken start                     # 默认 INFO，写 ~/.opentoken/logs/gateway.log
uv run opentoken start --log-level debug   # 加上浏览器 composer/点击策略等细节
uv run opentoken start --log-file none     # 只打终端
tail -f ~/.opentoken/logs/gateway.log
```

每行都带 `[request_id]`，和响应头 `X-Request-Id` 一致（客户端自带 `X-Request-Id` 时沿用），所以一次慢请求的完整链路可以用一个 id 串起来：

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

注意 `request_complete` 对 SSE 只表示**响应头已发出**（`streaming=true` 时 `elapsed_ms` 不是总耗时），真正的总耗时看 `stream_complete`。关键字段：

| 字段 | 含义 |
| --- | --- |
| `elapsed_ms` / `total_ms` | 各阶段耗时 |
| `first_token_ms` | 首字延迟（含浏览器会话/页面准备 + 上游思考） |
| `strategy` | 输入框填充方式；出现 `fill-fallback` 说明多行换行被压平，需要排查 |
| `doubao_submit_missing` | 点了发送但没发出 `/chat/completion`，自动重载重试一次 |
| `doubao_stream_path_failed` | 某条路径失败后切换到另一条（附 `detail`） |

环境变量：`OPENTOKEN_LOG_LEVEL`、`OPENTOKEN_LOG_FILE`（设为 `none` 关闭文件）。

## 常见排障

- **`/v1/models` 空** → 检查 `/health` + `opentoken providers`。无凭证的 provider 不出现在列表里。
- **总是 401** → 检查 `Authorization: Bearer` 是不是本地网关 key（`cat ~/.opentoken/config.json`）。
- **流式不稳定** → 用 `curl -N` 区分本地问题（直接失败/超时）vs 上游限流（明确的 rate-limit/error 事件）。
- **浏览器 provider 报 `NS_ERROR_PROXY_CONNECTION_REFUSED`** → Camoufox 读**系统级**代理（不受 `HTTP_PROXY` env 影响）；关掉系统代理或确保代理可达。纯 HTTP provider 不受影响。
- **某 provider 持续 429 / 验证码（如 Doubao）** → 上游对无头自动化的反爬，换个时间或换出口 IP 再试；网关侧会快速返回 429 而不是挂起。
- **切换监听地址** → `opentoken start --host 0.0.0.0` 会打非 loopback 警告。

## 开发与测试

```bash
./.venv/bin/pytest                # 全套（当前 669+ 用例）
./.venv/bin/pytest -k stream      # 流式相关
./.venv/bin/pytest tests/providers/  # provider 单测
./.venv/bin/pytest tests/storage/    # 存储原子性/权限
```

`tests/` 是离线单测(被 `pytest` 收集);`scripts/` 放需要真实凭证/网关的 **live & 压测脚本**(独立运行,不进单测):`live_provider_smoke.py`、`live_provider_200_suite.py`、`live_stream_regression.py`、`live_<provider>_200_cases.py`、`live_regression.py`、`live_doubao_regression.py`、`live_e2e_full.py`、`live_e2e_report.py`、`stress_test.py`。它们生成的 `*_report*.md` 等产物已被 gitignore。

## Git 提交前安全注意

`.gitignore` 已忽略 `.venv/`、`.opentoken/`、`tmp/`、`*.log`。**永远不要提交** `~/.opentoken/` 任何内容、导出的 cookie / token 文件、`.env*`、含 token 的调试日志。提交前先 `git status`，发现凭证文件就停。

## License

MIT —— 见 `LICENSE`。
