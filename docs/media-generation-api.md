# 图像 / 视频生成 API 协议文档

OpenToken 把豆包（Doubao）网页版「图像生成」（Seedream）和「视频生成」（Seedance）封装成 HTTP 接口。
上游**没有**图片 / 视频 API，全部由已登录的 Camoufox 浏览器会话驱动，因此请求会真实打开豆包页面、输入 prompt、点击发送、等待渲染完成。

| 能力 | 端点 | 上游 | 形态 |
|------|------|------|------|
| 图像生成 | `POST /v1/images/generations` | 豆包 图像生成 / Seedream | OpenAI `images.generations` 兼容，同步 |
| 视频生成（OpenAI SDK） | `POST /v1/videos` | 豆包 视频生成 / Seedance | OpenAI Sora `videos` 兼容，异步 job |
| 视频查询（OpenAI SDK） | `GET /v1/videos/{id}` | — | 轮询状态（含 `progress`） |
| 视频下载（OpenAI SDK） | `GET /v1/videos/{id}/content` | — | 代理返回 MP4 字节 |
| 视频列表（OpenAI SDK） | `GET /v1/videos` | — | `{"object":"list","data":[…]}` |
| 视频删除（OpenAI SDK） | `DELETE /v1/videos/{id}` | — | 丢弃 job 记录 |
| 视频生成（`/generations`） | `POST /v1/videos/generations` | 豆包 视频生成 / Seedance | 异步 job（默认）或 `wait=true` 同步 |
| 视频查询（`/generations`） | `GET /v1/videos/generations/{job_id}` | — | 轮询 job 状态 |

Base URL 默认 `http://127.0.0.1:32117`。

**目录**

1. [前置条件](#1-前置条件)
2. [认证](#2-认证)
3. [模型](#3-模型)
4. [图像生成](#4-图像生成)
5. [视频生成](#5-视频生成)
6. [错误响应](#6-错误响应)
7. [限制与注意事项](#7-限制与注意事项)
8. [排障速查](#8-排障速查)
9. [相关代码](#9-相关代码)

---

## 1. 前置条件

```bash
uv run opentoken login doubao        # 必须：媒体生成只用浏览器登录态
uv run opentoken start              # 默认 127.0.0.1:32117
```

- **必须先登录豆包**。未登录时媒体请求返回 `401 authentication_error`（消息里会提示跑 `opentoken login doubao`）。
- 登录后这 8 个模型才会出现在 `GET /v1/models` 里；未登录时 `/v1/models` 不列它们，以免 SDK 自动发现后调用必失败。这些条目的 `capability` 为 `image`（Seedream）或 `video`（Seedance），chat 模型则为 `text`，客户端据此选端点。
- 生成会消耗豆包账号的**每日额度**（Seedream Pro 4 倍消耗、Seedance 2.5 5 倍 / 2.0 2 倍）。额度用完时上游会回“今日免费额度已用完”，网关映射为 `429`。

## 2. 认证

与其它端点一致，用**本地网关自己的 API key**（不是上游 key）：

```
Authorization: Bearer <~/.opentoken/config.json 里的 api_key>
```

- `config.json` 不存在 → 开发模式 keyless 放行；
- `config.json` 存在但 `api_key` 为空 → **503 fail closed**；
- key 不匹配 → `401`。

所有响应都带 `X-Request-Id`，排障时拿它去搜网关日志。

## 3. 模型

### 3.1 图像模型

| 模型 ID | 豆包菜单标签 | 说明 | 别名（可省略 `doubao/` 前缀） |
|---------|-------------|------|------------------------------|
| `doubao/seedream-5.0-flash` | Seedream 5.0 Flash | **默认**，专业快速 | `seedream-5.0` · `seedream-5` · `seedream-5-flash` |
| `doubao/seedream-5.0-pro` | Seedream 5.0 Pro | 专业出图，4 倍消耗 | `seedream-5-pro` |
| `doubao/seedream-4.5` | Seedream 4.5 | 日常生成 | `seedream-4-5` · `seedream-45` |
| `doubao/seedream-4.0` | Seedream 4.0 | 基础生图 | `seedream-4-0` · `seedream-40` · `seedream` |

### 3.2 视频模型

| 模型 ID | 豆包菜单标签 | 说明 | 别名 |
|---------|-------------|------|------|
| `doubao/seedance-2.0-mini` | Seedance 2.0 Mini | **默认**，日常生成 | `seedance-mini` |
| `doubao/seedance-2.0-fast` | Seedance 2.0 Fast | 快速出片 | `seedance-fast` |
| `doubao/seedance-2.0` | Seedance 2.0 | 进阶画面表现，2 倍消耗 | `seedance` |
| `doubao/seedance-2.5` | Seedance 2.5 | 旗舰视频标杆，5 倍消耗 | — |

模型名解析规则：

- 大小写不敏感；
- 接受完整 ID `doubao/seedream-4.5`、裸名 `seedream-4.5`、别名 `seedream-45`，以及旧的 `algae/doubao/seedream-4.5` 命名空间；
- **跨类型会报错**：给 `/v1/images/generations` 传 `seedance-2.0-mini` 会 `400`（反之亦然）；
- 未知模型 → `400`，错误消息里会列出该类目全部可用模型。

---

## 4. 图像生成

### 4.1 `POST /v1/images/generations`

#### 请求体

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|:---:|------|------|
| `prompt` | string | ✅ | — | 画面描述。空白字符串视为缺失，回 `400` |
| `model` | string | | `doubao/seedream-5.0-flash` | 见 3.1 |
| `n` | integer | | `1` | 生成张数，**上限 4**，超出按 4 处理；`< 1` 或非整数回 `400` |
| `response_format` | string | | `url` | `url` 或 `b64_json`，其它值回 `400` |
| `timeout` | number | | `240` | **整次请求**总预算（秒），上限 `600`；超时回 `504` |

> `n > 1` 时网关会**串行**跑 N 轮浏览器会话（网页一次只能渲染一张），总耗时约 `n × 25~40s`，共享同一个 `timeout` 预算。
> OpenAI 的 `size` / `quality` / `style` / `negative_prompt` / `user` 等字段目前**不支持，会被静默忽略**（豆包网页没有对应控件，尺寸由模型自行决定，通常是 2048×2048）。

#### 响应 `200`

```json
{
  "created": 1792579161,
  "model": "doubao/seedream-5.0-flash",
  "data": [
    {
      "url": "https://p6-flow-imagex-sign.byteimg.com/tos-cn-i-a9rns2rl98/rc_gen_image/....jpeg~tplv-a9rns2rl98-image_raw.png?x-expires=2107939154&x-signature=...",
      "revised_prompt": "一只戴墨镜的橘猫，扁平插画风格，纯色背景，简洁有力的轮廓，..."
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `created` | integer | Unix 秒 |
| `model` | string | 解析后的规范模型 ID |
| `data[].url` | string | `response_format=url` 时存在。豆包签名 CDN 直链，指向 `image_raw` 原图（约 2048×2048 PNG / JPEG） |
| `data[].b64_json` | string | `response_format=b64_json` 时存在，与 `url` 互斥。网关下载后 base64 内联 |
| `data[].revised_prompt` | string | 可选。豆包对 prompt 的改写版本（模型实际执行的提示词） |

**URL 有效期**：签名链接自带 `x-expires`，实测到 2036 年，可长期保存；但仍建议需要持久化时自行下载落盘。`b64_json` 走网关下载，一张 2048² PNG 约 3.3 MB（base64 后约 4.4 MB）。

#### 示例

curl（返回 URL）：

```bash
curl http://127.0.0.1:32117/v1/images/generations \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "doubao/seedream-5.0-flash",
    "prompt": "一只戴墨镜的橘猫，扁平插画风格，纯色背景",
    "n": 1,
    "response_format": "url"
  }'
```

curl（内联 base64，落盘成 PNG）：

```bash
curl -s http://127.0.0.1:32117/v1/images/generations \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a single red apple on a white table","response_format":"b64_json"}' \
| python3 -c "import base64,json,sys; open('apple.png','wb').write(base64.b64decode(json.load(sys.stdin)['data'][0]['b64_json']))"
```

OpenAI Python SDK：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:32117/v1", api_key="YOUR_LOCAL_GATEWAY_KEY")

result = client.images.generate(
    model="doubao/seedream-5.0-flash",
    prompt="一只戴墨镜的橘猫，扁平插画风格",
    n=1,
    size="1024x1024",   # 会被忽略，仅为兼容旧客户端调用签名
)
print(result.data[0].url)
```

#### 典型耗时

| 场景 | 实测 |
|------|------|
| 单张 `url` | ~26–41 s |
| 单张 `b64_json`（含下载 + base64） | ~26 s / 3.3 MB |

---

## 5. 视频生成

视频是豆包侧真正意义上的**异步**投递：提交后上游先回一条「本次使用 Seedance 2.0 Mini 生成，预计等待 5 分钟…」的确认，几分钟后才发出视频卡片。因此默认设计成 job + 轮询。

⚠️ **参数确认轮**：豆包有时不直接开始渲染，而是先回一条参数复述并要求确认，例如

```
我将为你生成一只小猫在草地上奔跑的写实短视频，请先确认以下参数：
- 模型：Seedance 2.0 Mini
- 时长：10 秒
- 分辨率：720p（按 1280×720 宽屏制作）
确认后我再开始生成视频。
```

这时上游**不会**产出视频卡片，一直等到你下一轮回复。网关会识别这类回复并自动替你回一条「确认，请按上述参数开始生成视频。」，然后继续轮询（详见 [5.7](#57-参数确认轮自动应答)）。

### 5.1 `POST /v1/videos/generations`

#### 请求体

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|:---:|------|------|
| `prompt` | string | ✅ | — | 视频描述。空白回 `400` |
| `model` | string | | `doubao/seedance-2.0-mini` | 见 3.2 |
| `wait` | boolean | | `false` | `false` 立即返回 job；`true` 阻塞到渲染结束再返回结果。也接受字符串 `"true"` / `"false"`、`1` / `0`、`"yes"` / `"no"`（curl 用户容易写成字符串）；无法识别时按 `false` 处理 |
| `timeout` | number | | `600` | 渲染预算（秒），上限 `900`；超时回 `504`（`wait=true` 时）或 job 变 `failed` |

### 5.2 异步模式（默认，`wait=false`）

响应 `200`：立即返回 job，`status` 恒为 `in_progress`。

```json
{
  "id": "vidgen-65a7dbb099304d459ba26544658665ca",
  "object": "video.generation",
  "status": "in_progress",
  "created": 1792579205,
  "model": "doubao/seedance-2.0-mini",
  "prompt": "一只小猫在草地上奔跑，阳光明媚，写实风格",
  "status_url": "http://127.0.0.1:32117/v1/videos/generations/vidgen-65a7dbb099304d459ba26544658665ca"
}
```

### 5.3 `GET /v1/videos/generations/{job_id}`

轮询用。状态机：

```
in_progress ──► completed
     └───────► failed
```

**`in_progress` 200**：与上面创建响应同构。

**`completed` 200**：

```json
{
  "id": "vidgen-65a7dbb099304d459ba26544658665ca",
  "object": "video.generation",
  "status": "completed",
  "created": 1792579205,
  "model": "doubao/seedance-2.0-mini",
  "prompt": "一只小猫在草地上奔跑，阳光明媚，写实风格",
  "url": "https://v26-vdl.doubao.com/.../video/tos/cn/tos-cn-v-9ecd54/....?mime_type=video_mp4&...",
  "width": 1280,
  "height": 720,
  "duration_seconds": 10.08,
  "completed_at": 1792579367,
  "status_url": "http://127.0.0.1:32117/v1/videos/generations/vidgen-65a7dbb099304d459ba26544658665ca"
}
```

**`failed` 200**：

```json
{
  "id": "vidgen-....",
  "object": "video.generation",
  "status": "failed",
  "created": 1792579205,
  "model": "doubao/seedance-2.0-mini",
  "prompt": "....",
  "error": {
    "message": "Doubao video generation timed out after 600s without a playable video.",
    "type": "api_error"
  },
  "status_url": "http://127.0.0.1:32117/v1/videos/generations/vidgen-...."
}
```

**未知 job** → `404` `invalid_request_error`。

字段说明：

| 字段 | 出现时机 | 说明 |
|------|---------|------|
| `id` | 始终 | `vidgen-<32 hex>` |
| `object` | 始终 | 固定 `video.generation` |
| `status` | 始终 | `in_progress` / `completed` / `failed` |
| `created` | 始终 | 提交时间（Unix 秒） |
| `model` / `prompt` | 始终 | 回显 |
| `status_url` | 始终 | 该 job 的轮询地址（绝对 URL） |
| `url` | completed | 豆包签名 MP4 直链，`Range` 请求可用，可直接播/下载 |
| `width` / `height` | completed | 实测常见 `1280 × 720` |
| `duration_seconds` | completed | 浮点秒，如 `10.08` |
| `completed_at` | completed | 完成时间（Unix 秒） |
| `error.message` / `error.type` | failed | 失败原因，`type` 目前恒为 `api_error` |

### 5.4 同步模式（`wait=true`）

阻塞到渲染结束，直接返回完成结果（**不是** job 信封）：

```json
{
  "created": 1792579367,
  "model": "doubao/seedance-2.0-mini",
  "data": [
    {
      "url": "https://v26-vdl.doubao.com/....?mime_type=video_mp4&...",
      "width": 1280,
      "height": 720,
      "duration": 10.08
    }
  ]
}
```

注意：同步模式字段名是 `data[].duration`，异步 job 是顶层 `duration_seconds`（历史原因，两者都保留）。

`wait=true` 期间失败**不走 job**，直接按错误表返回 HTTP 状态码（429 / 504 / 502 / 401 / 400）。

### 5.5 示例

异步 + 轮询（推荐）：

```bash
# 1) 提交
curl -s http://127.0.0.1:32117/v1/videos/generations \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"doubao/seedance-2.0-mini","prompt":"一只小猫在草地上奔跑，阳光明媚，写实风格"}'
# → {"id":"vidgen-....","status":"in_progress","status_url":"..."}

# 2) 轮询
watch -n 5 'curl -s http://127.0.0.1:32117/v1/videos/generations/vidgen-.... \
  -H "Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY" | python3 -m json.tool'
```

同步：

```bash
curl -s http://127.0.0.1:32117/v1/videos/generations \
  -H 'Authorization: Bearer YOUR_LOCAL_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"doubao/seedance-2.0-mini","prompt":"一只小猫在草地上奔跑","wait":true,"timeout":600}'
```

Python（异步轮询，带退避）：

```python
import time

import httpx

BASE = "http://127.0.0.1:32117"
HEADERS = {"Authorization": "Bearer YOUR_LOCAL_GATEWAY_KEY"}

with httpx.Client(base_url=BASE, headers=HEADERS, timeout=60) as client:
    job = client.post(
        "/v1/videos/generations",
        json={"model": "doubao/seedance-2.0-mini", "prompt": "一只小猫在草地上奔跑"},
    ).json()
    print("job:", job["id"])

    delay = 5.0
    while True:
        state = client.get(f"/v1/videos/generations/{job['id']}").json()
        if state["status"] == "completed":
            print(state["url"], state["width"], state["height"], state["duration_seconds"])
            break
        if state["status"] == "failed":
            raise RuntimeError(state["error"]["message"])
        time.sleep(delay)
        delay = min(delay * 1.5, 30.0)   # 5s → 7.5s → … → 30s
```

### 5.6 典型耗时

| 模型 | 实测 |
|------|------|
| `doubao/seedance-2.0-mini`（720p / 10s） | ~165 s（上游提示「预计等待 5 分钟」）；忙时可达 5–9 分钟 |

建议轮询间隔 5–15 s，别用 1 s 贴脸轮询（job 只在进程内存里，频繁读没意义）。`timeout` 默认 600 s，若经常等到 `failed`，调到 `900`。

### 5.7 参数确认轮（自动应答）

豆包的视频技能不是每次都直接开渲染。当它判断需要对齐参数时，会先回一条「请先确认以下参数…确认后我再开始生成视频」，然后**停在对话里等你回话**。

对纯 HTTP 调用方来说这一步是死锁：没有后续 turn → 永远等不到视频卡片 → 只能等到 `timeout` 报错，而实际上上游根本还没开始渲染。

网关的处理：

1. 每轮轮询都会扫新增的助手消息，命中确认类措辞就自动回一条
   `确认，请按上述参数开始生成视频。`（生图场景是 `…开始生成图片。`）；
2. **要求该轮回复在两次轮询之间稳定不变**才回话——正在流式输出的半截回复不会触发，避免和上游抢同一个 turn；
3. **同一轮 turn 只回一次**，且最多回 **3 轮**；超过就立刻失败并报
   `Doubao keeps asking for confirmation without starting the render`，不会把额度耗在无限确认上；
4. 整个过程只进日志，HTTP 响应形状不变：

```
doubao_media_confirmation_required kind=视频 round=1 text=我将为你生成…请先确认以下参数…
doubao_media_confirmation_sent kind=视频 round=1
```

判定用的是多字短语（`确认后我再开始` / `请先确认以下` / `确认以下参数` / `参数确认` / `确认无误后` …），**不是**裸的「确认」：像「本次生成将消耗每日免费额度，请确认后继续等待」这种开场白表示“已开始”，回话会导致重复提交第二个渲染任务，因此被刻意排除。

调用方不需要做任何事；如果你想自己控制参数，就在 `prompt` 里把时长 / 分辨率写清楚（如「10 秒、1280x720 横屏」），能减少上游发起确认的概率。

### 5.8 OpenAI Sora 形状：`/v1/videos`

`openai` 官方 SDK 的 `client.videos.create()` 打的是 `POST /v1/videos`（不是 `/generations`）。之前只注册了 `/generations`，所以这个路径会落到 FastAPI 默认的 **`404 {"detail":"Not Found"}`**——这是最常被误判成“豆包生成有问题”的报错。现在两种形状并存，共用同一套渲染与 job 存储。

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/v1/videos` | 创建任务，返回 OpenAI `Video` 对象 |
| `GET` | `/v1/videos/{id}` | 查询单个任务 |
| `GET` | `/v1/videos/{id}/content` | 下载已完成的 MP4（网关代理上游字节） |
| `GET` | `/v1/videos` | 列出任务，新的在前 |
| `DELETE` | `/v1/videos/{id}` | 丢弃 job 记录 |

创建请求体接受 **JSON 或 multipart/form-data**（SDK 带 `input_reference` 时用 multipart）：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|:---:|------|------|
| `prompt` | string | ✅ | — | 视频描述 |
| `model` | string | | `doubao/seedance-2.0-mini` | 见 3.2 |
| `size` | string | | `null` | 形如 `1280x720`；格式非法回 `400`。豆包自行决定实际分辨率，完成后会用真实值回填 |
| `seconds` | string/number | | `null` | 正数；完成后用实际时长回填 |
| `wait` | boolean | | `false` | 扩展字段：`true` 阻塞到完成并直接返回 `completed` 的 Video 对象 |
| `input_reference` | file | ❌ | — | **不支持**，显式回 `400`：豆包网页版技能只做文生视频，静默忽略会让调用方拿到完全不同的片子 |

`POST /v1/videos` 响应 `200`（`video` 对象，`status` 为 `in_progress` / `completed` / `failed`）：

```json
{
  "id": "vidgen-baaa51bbfcd944d48a48506ca86da678",
  "object": "video",
  "model": "doubao/seedance-2.0-mini",
  "status": "in_progress",
  "progress": 1,
  "created_at": 1792585759,
  "seconds": "10",
  "size": "1280x720",
  "remixed_from_video_id": null,
  "error": null
}
```

- `progress`：0–100 的**估算**（按 300 s 预期线性推进，上游不报百分比）；完成时 `100`，失败时 `0`。
- `error`：`failed` 时是 `{"code": "generation_failed", "message": "..."}`，其余为 `null`。
- 完成后额外带 `completed_at` 与 `url`（豆包签名 CDN 直链）。

`GET /v1/videos/{id}/content`：

- 未完成 → `409` `invalid_request_error`，`code=video_not_ready`；
- 已完成 → `200`，`Content-Type: video/mp4`，`Content-Disposition: attachment; filename="<id>.mp4"`；
- 上游取流失败 → `502` `api_error`。

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:32117/v1", api_key="YOUR_LOCAL_GATEWAY_KEY")

video = client.videos.create(
    model="doubao/seedance-2.0-mini",
    prompt="一只小猫在草地上奔跑，阳光明媚，写实风格",
    size="1280x720",
    seconds="10",
)
print(video.id, video.status, video.progress)

while video.status in ("in_progress", "queued"):
    video = client.videos.retrieve(video.id)
    print(video.status, video.progress)
    time.sleep(10)

content = client.videos.download_content(video.id)
content.write_to_file("cat.mp4")
```

---

## 6. 错误响应

所有错误统一 OpenAI 形状：

```json
{
  "error": {
    "message": "...",
    "type": "invalid_request_error",
    "param": "prompt"
  }
}
```

`param` 只在能定位到具体字段时出现。

| HTTP | `error.type` | 触发场景 |
|:---:|--------------|---------|
| `400` | `invalid_request_error` | `prompt` 缺失/空白；`n` / `size` / `seconds` / `timeout` 非法；未知模型；跨类型模型；`response_format` 非法；`/v1/videos` 传了 `input_reference` |
| `401` | `authentication_error` | 网关 API key 错误；豆包未登录 / 会话过期（需重新 `opentoken login doubao`） |
| `404` | `invalid_request_error` | 视频 job 不存在（含网关重启后），`code=video_not_found` |
| `409` | `invalid_request_error` | `GET /v1/videos/{id}/content` 时任务还没完成，`code=video_not_ready` |
| `413` | `invalid_request_error` | 请求体超过 25 MiB |
| `429` | `rate_limit_error` | 触发上游限流；豆包每日免费额度用完 |
| `502` | `api_error` | 上游 5xx / 页面结构变化找不到模型菜单或媒体卡片；生成失败（如“不符合平台规范”）；`b64_json` 下载失败 |
| `503` | `api_error` | `config.json` 存在但 `api_key` 为空（fail closed），或配置损坏不可读 |
| `504` | `api_error` | 超过 `timeout` 仍未出结果（消息含 `timed out`） |

错误消息**不会**泄漏上游 URL / cookie / session；详细堆栈只进网关日志（用 `X-Request-Id` 关联）。

---

## 7. 限制与注意事项

1. **浏览器串行**：媒体生成走同一个豆包浏览器会话，加的是 provider 级互斥锁。并发发多个生图/生视频请求会**排队**，不会并行；排在后面的请求要自己承担等待时间（也计入各自的 `timeout`）。
2. **`n` 上限 4**：`n` 是串行多轮，不是原生 batch。要更多图请自行并发多请求（仍然会被锁串行化）。
3. **会话污染**：跑完媒体任务后网关会重新加载豆包聊天页（`https://www.doubao.com/chat/`）把 composer 复位回「对话」模式，所以之后的 chat 请求不会误输入到图像/视频模式；这也意味着媒体任务完成后第一次 chat 会稍慢。
4. **job 只在内存里**：视频 job 存于网关进程内存，TTL 1 h，超过 64 条时按 TTL 清理。网关重启 → 旧 `job_id` 查不到（`404`），但视频仍留在你的豆包会话里，可以手动去网页找。
5. **一次一请求**：`wait=true` 会占用一个 HTTP 连接几分钟，注意客户端超时要大于 `timeout`。
6. **额度与成本**：Seedream 5.0 Pro 4×、Seedance 2.0 2×、Seedance 2.5 5× 消耗。额度不足时是 `429`，不是 `400`。
7. **UI 依赖**：整条链路依赖豆包网页 DOM（`skill_bar_button_*`、媒体卡片、播放器）。豆包改版后可能返回 `502` 且消息形如 “Doubao media model '...' not found in the model menu.” / “Doubao skill '图像生成' is not available”，此时需要更新 `src/opentoken/providers/doubao_media.py` 里的选择器。
8. **无 `seed` / 无 `negative_prompt`**：网页没有这些控件，无法透传；同样的 prompt 出图不保证可复现。
9. **`size` / `seconds` 只是提示**：`/v1/videos` 会校验并回显，但豆包网页版技能不接受程序化控制，实际分辨率/时长由上游决定；完成后响应里回填的是**真实**值。
10. **`input_reference`（图生视频）不支持**：网页版技能只做文生视频，传了会显式 `400`，不会静默忽略。
11. **参数确认轮会自动应答**（见 5.7）：如果业务上不希望网关替你确认（例如要人工核对参数后再花额度），目前只能先用 `/v1/videos` 的 `wait=true` + 较小 `timeout` 之类的粗粒度控制，或改用 `opentoken login` 后的人工网页操作。

## 8. 排障速查

| 现象 | 处理 |
|------|------|
| `401 ... Run \`opentoken login doubao\` first.` | 重新登录豆包 |
| `400 Unknown image model: xxx` | 从 3.1 的表中选；或先查 `/v1/models` |
| `502 Doubao skill '图像生成' is not available` | 账号未开放该技能 / 页面改版，先在浏览器里手动确认能用 |
| `502 ... produced no images` | 生成被拒或页面未渲染，检查 prompt 是否违规、看日志里的 last reply |
| 视频一直 `in_progress` 直到 `failed` | 多半超时；提高 `timeout`（≤900）或换 `seedance-2.0-fast`。若日志里没有 `doubao_media_confirmation_required`，说明确实在渲染，只是上游排队 |
| `failed ... keeps asking for confirmation` | 上游连续 3 轮都在要求确认（通常 prompt 太模糊）。把时长 / 分辨率 / 画面写具体后重试 |
| `404 {"detail":"Not Found"}` 且**没有** `error` 包装 | 路径不存在。视频请用 `/v1/videos` 或 `/v1/videos/generations`，注意别写成 `/v1/video/generations` |
| `409 video_not_ready` | `GET /v1/videos/{id}/content` 太早，先轮询到 `status=completed` |
| `404 job not found` | 网关重启过，重新提交 |
| `429 ... 今日免费额度已用完` | 等次日额度刷新，或换账号 |

## 9. 相关代码

| 文件 | 职责 |
|------|------|
| `src/opentoken/api/routes/images.py` | `/v1/images/generations` 参数校验、b64 下载、错误映射 |
| `src/opentoken/api/routes/videos.py` | `/v1/videos`（OpenAI Sora 形状）与 `/v1/videos/generations` 两套形状共用 job 存储、`/content` 字节代理、参数校验与错误映射 |
| `src/opentoken/models/media.py` | 模型目录、别名解析、登录态过滤 |
| `src/opentoken/providers/doubao_media.py` | 豆包页面驱动：切技能、选模型、SSE 抓图、等视频卡片、点播放器取 MP4 |
| `src/opentoken/providers/camoufox_clients.py` | 浏览器会话复用、`generate_images` / `generate_video` 入口、composer 复位 |
| `src/opentoken/gateway/router.py` | `generate_images` / `generate_video` 路由到浏览器客户端 |

测试：`tests/api/test_images_route.py`、`tests/api/test_videos_route.py`、`tests/models/test_media_models.py`、`tests/providers/test_doubao_media.py`。
