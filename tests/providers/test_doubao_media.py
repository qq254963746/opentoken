import json
import re
import time
from pathlib import Path

import pytest

from opentoken.models.media import resolve_media_model
from opentoken.providers.base import ProviderRateLimitError
from opentoken.providers.doubao_media import (
    MediaAsset,
    _label_pattern,
    extract_doubao_creation_images,
    generate_doubao_images,
    generate_doubao_video,
)

_DISCRETE_DURATION_PATTERN = re.compile(r"^\d{1,2}s$")


def _sse(*events: dict) -> str:
    lines = []
    for index, event in enumerate(events):
        lines.append(f"id: {index}")
        lines.append("event: STREAM_CHUNK")
        lines.append("data: " + json.dumps(event, ensure_ascii=False))
        lines.append("")
    return "\n".join(lines)


def _creation_event(
    *,
    status: int,
    raw_url: str | None = None,
    thumb_url: str | None = None,
    prompt: str | None = None,
    width: int = 2048,
    height: int = 2048,
) -> dict:
    image: dict[str, object] = {
        "key": "tos-cn-i-a9rns2rl98/rc_gen_image/abc.jpeg",
        "status": status,
        "image_thumb": {"url": thumb_url or "https://cdn.example/abc~thumb.png"},
    }
    if raw_url:
        image["image_ori_raw"] = {"url": raw_url, "width": width, "height": height}
    if prompt:
        image["gen_params"] = {"prompt": prompt, "ratio": "1:1"}
    return {
        "content_block": [
            {
                "block_type": 2074,
                "content": {"creation_block": {"creations": [{"type": 1, "image": image}]}},
            }
        ]
    }


def test_extract_creation_images_uses_the_finished_raw_variant() -> None:
    body = _sse(
        _creation_event(status=1),
        _creation_event(
            status=2,
            raw_url="https://cdn.example/abc~image_raw.png",
            thumb_url="https://cdn.example/abc~thumb.png",
            prompt="一只戴墨镜的猫，扁平插画风格",
            width=2048,
            height=2048,
        ),
    )

    assets = extract_doubao_creation_images(body)

    assert len(assets) == 1
    asset = assets[0]
    assert asset.url == "https://cdn.example/abc~image_raw.png"
    assert asset.width == 2048
    assert asset.height == 2048
    assert asset.media_type == "image/png"
    assert asset.revised_prompt == "一只戴墨镜的猫，扁平插画风格"


def test_extract_creation_images_ignores_placeholders_and_duplicates() -> None:
    event = _creation_event(status=2, raw_url="https://cdn.example/abc~image_raw.png")
    body = _sse(
        _creation_event(status=1),
        event,
        event,
    )

    assets = extract_doubao_creation_images(body)

    assert [asset.url for asset in assets] == ["https://cdn.example/abc~image_raw.png"]


def test_extract_creation_images_tolerates_non_json_and_empty_bodies() -> None:
    assert extract_doubao_creation_images("") == []
    assert extract_doubao_creation_images("data: not-json\n\nevent: SSE_HEARTBEAT\ndata: {}\n\n") == []


def test_label_pattern_does_not_swallow_longer_sibling_models() -> None:
    base = _label_pattern("Seedance 2.0")
    assert base.match("Seedance 2.0 升级 进阶画面表现")
    assert not base.match("Seedance 2.0 Fast 快速出片选择")
    assert not base.match("Seedance 2.0 Mini 日常生成使用")

    mini = _label_pattern("Seedance 2.0 Mini")
    assert mini.match("Seedance 2.0 Mini")
    assert not mini.match("Seedance 2.0 Fast")

    assert _label_pattern("Seedream 5.0 Flash").match("Seedream 5.0 Flash 专业快速")


class FakeKeyboard:
    def press(self, key: str) -> None:
        return None

    def type(self, text: str, delay: int = 0) -> None:
        return None


class FakeKeyboardWithSlider(FakeKeyboard):
    """Keyboard that actually moves the duration slider when it has focus."""

    def __init__(self, page: "FakePage") -> None:
        self._page = page

    def press(self, key: str) -> None:
        page = self._page
        if not page.slider_focused:
            return
        if key == "ArrowRight":
            page.slider_value = min(11, page.slider_value + 1)
            page.video_seconds = page.slider_seconds(page.slider_value)
        elif key == "ArrowLeft":
            page.slider_value = max(0, page.slider_value - 1)
            page.video_seconds = page.slider_seconds(page.slider_value)
        elif key == "Home":
            page.slider_value = 0
            page.video_seconds = page.slider_seconds(0)


class FakeLocator:
    """Playwright-like locator over :class:`FakePage` state."""

    def __init__(
        self,
        page: "FakePage",
        selector: str = "",
        has_text=None,
        role: str | None = None,
    ) -> None:
        self._page = page
        self.selector = selector
        self.has_text = has_text
        self.role = role
        if '[role="slider"]' in selector:
            page.slider_locator_uses += 1

    @property
    def first(self) -> "FakeLocator":
        return self

    @property
    def last(self) -> "FakeLocator":
        return self

    def count(self) -> int:
        if "attachment-delete-btn" in self.selector:
            return self._page.attachments
        if "attachment-image-card" in self.selector:
            return self._page.attachments
        if "input-engine-container button" in self.selector:
            # The chip must exist for the options flow to proceed.
            return 1
        return 1

    def filter(self, has_text=None) -> "FakeLocator":
        return FakeLocator(self._page, self.selector, has_text, self.role)

    def click(self, timeout=None, force=None) -> None:
        label = str(self.has_text or "")
        self._page.clicks.append((self.role or self.selector, label))
        # Model reality: clicking a skill chip swaps the composer placeholder,
        # which is what `_enter_skill` waits for (and why it can skip the click
        # when the composer is already in that mode).
        if "图像生成" in label:
            self._page.placeholder = "描述你想要的图片"
        elif "视频生成" in label:
            self._page.placeholder = "描述你想要的视频"
        if "attachment-delete-btn" in self.selector and self._page.attachments:
            self._page.attachments -= 1
        # Ratio buttons: `get_by_text("16:9", exact=True)`.
        if self.selector == "text" and label:
            if _DISCRETE_DURATION_PATTERN.match(label):
                # Discrete "5s" duration buttons (newer panels).
                self._page.video_seconds = int(label[:-1])
            else:
                self._page.video_ratio = label

    def focus(self) -> None:
        if "role=\"slider\"" in self.selector or self.role == "slider":
            self._page.slider_focused = True

    def get_attribute(self, name: str):
        if "slider" in self.selector.lower() or self.role == "slider":
            if name == "aria-valuenow":
                return str(self._page.slider_value)
            if name == "aria-valuemin":
                return "0"
            if name == "aria-valuemax":
                return "11"
        return None

    def all_inner_texts(self) -> list[str]:
        return [
            f"模型 {self._page.model_label}",
            f"{self._page.video_ratio} · {self._page.video_seconds}s",
        ]

    def set_input_files(self, files, timeout=None) -> None:
        if isinstance(files, str):
            files = [files]
        self._page.uploaded.append([str(path) for path in files])
        self._page.attachments += len(files)

    def wait_for(self, timeout=None) -> None:
        return None

    def fill(self, value: str) -> None:
        self._page.filled.append(value)

    def inner_text(self, timeout=None) -> str:
        if "模型" in str(self.has_text) or "模型" in self.selector:
            return self._page.model_label
        return ""


class FakePage:
    """Just enough Playwright surface for the media flows."""

    def __init__(
        self,
        *,
        model_label: str,
        placeholder: str,
        media_states: list[dict] | None = None,
        video_srcs: list[list[str]] | None = None,
        sse_body: str = "",
    ) -> None:
        self.model_label = model_label
        self.placeholder = placeholder
        # Start in plain chat mode; the skill chip is what switches modes.
        self.target_placeholder = placeholder
        self.placeholder = "发消息或按住空格说话..."
        self.media_states = list(media_states or [])
        self.last_media_state = (media_states or [{}])[-1]
        # Transcript turn count *before* this request. Real calls read this from
        # the live page; the fake must not derive it from the upcoming states or
        # every poll already looks like "new turns exist".
        self.baseline_turn_count = 0
        self.video_srcs = list(video_srcs or [])
        self.sse_body = sse_body
        # One body per send: a confirmation turn consumes one, and the render's
        # SSE arrives on a later turn.
        self.sse_bodies = list(sse_body) if isinstance(sse_body, list) else [sse_body]
        self.send_clicks = 0
        # Pending "确认生成" consent buttons, oldest first. Each entry is what the
        # real JS returns after clicking, so tests can model the compliance gate
        # (no turn text) and the paid-quota offer (turn text + button).
        self.consent_buttons: list[dict] = []
        self.consent_clicks = 0
        self.clicks: list[tuple[str, str]] = []
        self.filled: list[str] = []
        self.listeners: dict[str, object] = {}
        self.evaluated_media_queries = 0
        # Video options: the composer chip reads "<ratio> · <seconds>s", and the
        # duration slider runs aria-valuenow 0..11 for 4s..15s.
        self.video_ratio = "自动"
        self.video_seconds = 10
        self.slider_value = 6
        self.slider_focused = False
        # Set to a non-None int to model a panel whose aria numbering maps to a
        # different second than `4 + now` (the case that produced the 9s/5s bug).
        self.slider_seconds_override: int | None = None
        # Newer panels replace the slider with discrete "5s"/"9s" buttons.
        self.panel_slider = True
        self.panel_durations: list[str] = []
        # Recorded so tests can prove no page-wide slider lookup happens.
        self.slider_locator_uses = 0
        self.panel_open = False
        self.attachments = 0
        self.uploaded: list[list[str]] = []
        self.keyboard = FakeKeyboardWithSlider(self)

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        return FakeLocator(self, "text", text)

    def get_by_role(self, role: str) -> FakeLocator:
        return FakeLocator(self, "", None, role)

    def wait_for_timeout(self, timeout_ms: int) -> None:
        return None

    def slider_seconds(self, aria_value: int) -> int:
        """What the composer chip shows for an aria-valuenow position."""
        if self.slider_seconds_override is not None:
            return self.slider_seconds_override + aria_value
        return 4 + aria_value

    def on(self, event_name: str, callback) -> None:
        self.listeners[event_name] = callback

    def remove_listener(self, event_name: str, callback) -> None:
        self.listeners.pop(event_name, None)

    def evaluate(self, script: str, arg=None):
        if "确认生成" in script:  # _MEDIA_CONSENT_BUTTON_JS
            if not self.consent_buttons:
                self.last_consent_probe_empty = True
                return {"clicked": False, "reason": "no-button"}
            self.consent_clicks += 1
            entry = dict(self.consent_buttons.pop(0))
            entry["clicked"] = True
            return entry
        if "比例" in script and "时长" in script:
            # Every video-options probe is panel-scoped; the focus variant also
            # returns the focused slider's state at the top level.
            self.panel_open = True
            if "focus()" in script:  # _FOCUS_DURATION_SLIDER_JS
                self.slider_focused = True
                return {
                    "open": True,
                    "focused": True,
                    "sliderCount": 1,
                    "now": self.slider_value,
                    "min": 0,
                    "max": 11,
                    "label": "时长",
                    "text": f"{self.video_seconds}s",
                }
            return {
                "open": True,
                "ratios": ["自动", "3:4", "4:3", "9:16", "16:9", "1:1", "21:9"],
                "durations": list(self.panel_durations),
                "sliderCount": 1 if self.panel_slider else 0,
                "slider": (
                    {
                        "now": self.slider_value,
                        "min": 0,
                        "max": 11,
                        "label": "时长",
                        "text": f"{self.video_seconds}s",
                    }
                    if self.panel_slider
                    else None
                ),
            }
        if "videoPlayers" in script:  # _MEDIA_STATE_JS
            self.evaluated_media_queries += 1
            if self.media_states:
                self.last_media_state = self.media_states.pop(0)
            return self.last_media_state
        if "data-placeholder" in script:
            return self.placeholder
        if "video-player-wrapper" in script:
            return True
        if "document.querySelectorAll('video')" in script:
            return self.video_srcs.pop(0) if self.video_srcs else []
        if 'receive_message' in script:
            return self.baseline_turn_count
        if "g-send-msg-btn-bg" in script:
            self.send_clicks += 1
            callback = self.listeners.get("response")
            if callable(callback):
                body = self.sse_bodies.pop(0) if self.sse_bodies else ""
                callback(FakeResponse(body))
            return True
        return None


class FakeResponse:
    def __init__(self, body: str) -> None:
        self.url = "https://www.doubao.com/chat/completion?aid=497858"
        self.request = type("Request", (), {"method": "POST"})()
        self.headers = {"content-type": "text/event-stream"}
        self.status = 200
        self._body = body

    def text(self) -> str:
        return self._body


def test_generate_doubao_images_prefers_the_sse_raw_render() -> None:
    sse = _sse(
        _creation_event(status=1),
        _creation_event(
            status=2,
            raw_url="https://cdn.example/abc~image_raw.png",
            thumb_url="https://cdn.example/abc~thumb.png",
            prompt="重写后的提示词",
        ),
    )
    page = FakePage(
        model_label="Seedream 5.0 Flash",
        placeholder="描述你想要的图片",
        media_states=[
            {
                "turnCount": 1,
                "imageUrls": [],
                "imageFinished": False,
                "videoCoverCount": 0,
                "videoPlayers": [],
                "texts": [],
            },
            {
                "turnCount": 1,
                "imageUrls": ["https://cdn.example/abc~thumb.png"],
                "imageFinished": True,
                "videoCoverCount": 0,
                "videoPlayers": [],
                "texts": ["已生成。"],
            },
            {
                "turnCount": 1,
                "imageUrls": ["https://cdn.example/abc~thumb.png"],
                "imageFinished": True,
                "videoCoverCount": 0,
                "videoPlayers": [],
                "texts": ["已生成。"],
            },
        ],
        sse_body=sse,
    )

    assets = generate_doubao_images(
        page,
        prompt="一只戴墨镜的猫",
        model=resolve_media_model("doubao/seedream-5.0-flash", kind="image"),
        count=1,
        timeout_seconds=30,
        per_image_timeout_seconds=30,
    )

    assert len(assets) == 1
    assert assets[0].url == "https://cdn.example/abc~image_raw.png"
    assert assets[0].revised_prompt == "重写后的提示词"
    assert any(
        "skill_bar_button_" in selector and "图像生成" in has_text
        for selector, has_text in page.clicks
    )


def test_generate_doubao_images_falls_back_to_dom_thumbnails() -> None:
    page = FakePage(
        model_label="Seedream 5.0 Flash",
        placeholder="描述你想要的图片",
        media_states=[
            {
                "turnCount": 1,
                "imageUrls": ["https://cdn.example/abc~thumb.png"],
                "imageFinished": True,
                "videoCoverCount": 0,
                "videoPlayers": [],
                "texts": [],
            },
            {
                "turnCount": 1,
                "imageUrls": ["https://cdn.example/abc~thumb.png"],
                "imageFinished": True,
                "videoCoverCount": 0,
                "videoPlayers": [],
                "texts": [],
            },
        ],
        sse_body="",
    )

    assets = generate_doubao_images(
        page,
        prompt="一只猫",
        model=resolve_media_model("seedream-4.5", kind="image"),
        count=1,
        timeout_seconds=30,
        per_image_timeout_seconds=30,
    )

    assert [asset.url for asset in assets] == ["https://cdn.example/abc~thumb.png"]


def test_generate_doubao_images_surfaces_a_refusal() -> None:
    page = FakePage(
        model_label="Seedream 5.0 Flash",
        placeholder="描述你想要的图片",
        media_states=[
            {
                "turnCount": 1,
                "imageUrls": [],
                "imageFinished": False,
                "videoCoverCount": 0,
                "videoPlayers": [],
                "texts": ["生成失败，请稍后再试。"],
            }
        ],
    )

    with pytest.raises(RuntimeError, match="生成失败"):
        generate_doubao_images(
            page,
            prompt="一只猫",
            model=resolve_media_model("seedream-4.5", kind="image"),
            count=1,
            timeout_seconds=30,
            per_image_timeout_seconds=30,
        )


def test_select_media_model_reopens_the_menu_when_the_label_differs() -> None:
    from opentoken.providers.doubao_media import _label_pattern, _select_media_model

    page = FakePage(model_label="Seedance 2.0 Fast", placeholder="描述你想要的视频")

    _select_media_model(page, "Seedance 2.0")

    assert ("#input-engine-container button", "模型") in page.clicks
    assert any(
        role == "menuitem" and str(has_text) == str(_label_pattern("Seedance 2.0"))
        for role, has_text in page.clicks
    )


def test_select_media_model_keeps_the_current_selection() -> None:
    from opentoken.providers.doubao_media import _select_media_model

    page = FakePage(model_label="Seedance 2.0 Mini", placeholder="描述你想要的视频")

    _select_media_model(page, "Seedance 2.0 Mini")

    assert page.clicks == []


def test_generate_doubao_video_returns_the_playable_url() -> None:
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[
            {
                "turnCount": 1,
                "imageUrls": [],
                "imageFinished": False,
                "videoCoverCount": 0,
                "videoPlayers": [],
                "texts": ["本次使用 Seedance 2.0 Mini 生成，预计等待 5 分钟。"],
            },
            {
                "turnCount": 2,
                "imageUrls": [],
                "imageFinished": False,
                "videoCoverCount": 1,
                "videoPlayers": [],
                "texts": ["你的视频生成好了。"],
            },
            {
                "turnCount": 2,
                "imageUrls": [],
                "imageFinished": False,
                "videoCoverCount": 1,
                "videoPlayers": [
                    {
                        "src": "https://v11-vdl.doubao.com/video/tos/cn/abc/?mime_type=video_mp4",
                        "width": 1280,
                        "height": 720,
                        "duration": 10.08,
                    }
                ],
                "texts": ["你的视频生成好了。"],
            },
        ],
    )

    asset = generate_doubao_video(
        page,
        prompt="一只小猫在草地上奔跑",
        model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
        timeout_seconds=5,
        player_timeout_seconds=1,
    )

    assert isinstance(asset, MediaAsset)
    assert asset.url.endswith("mime_type=video_mp4")
    assert asset.media_type == "video/mp4"
    assert asset.width == 1280
    assert asset.height == 720
    assert asset.duration_seconds == pytest.approx(10.08)


def test_generate_doubao_video_times_out_with_diagnostics() -> None:
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[
            {
                "turnCount": 1,
                "imageUrls": [],
                "imageFinished": False,
                "videoCoverCount": 0,
                "videoPlayers": [],
                "texts": ["本次生成将消耗每日免费额度。"],
            }
        ],
        video_srcs=[[]],
    )

    with pytest.raises(RuntimeError, match="timed out"):
        generate_doubao_video(
            page,
            prompt="一只小猫",
            model=resolve_media_model("seedance-mini", kind="video"),
            timeout_seconds=0.2,
            player_timeout_seconds=0.1,
        )


# --------------------------------------------------------------------------- #
# "请先确认参数" turn: Doubao waits for a follow-up instead of rendering
# --------------------------------------------------------------------------- #
# Verbatim shape of the reply Doubao sends instead of starting the render.
_CONFIRMATION_REPLY = (
    "我将为你生成一只小猫在草地上奔跑的写实短视频，请先确认以下参数： "
    "视频生成参数确认 "
    "- 模型：Seedance 2.0 Mini "
    "- 时长：10 秒 "
    "- 分辨率：720p（按 1280x720 宽屏制作） "
    "确认后我再开始生成视频。"
)
_SUBMITTED_REPLY = "本次使用 Seedance 2.0 Mini 生成，预计等待 5 分钟。"


def _video_state(turn_count: int, texts: list[str], **overrides) -> dict:
    state = {
        "turnCount": turn_count,
        "imageUrls": [],
        "imageFinished": False,
        "videoCoverCount": 0,
        "videoPlayers": [],
        "texts": texts,
    }
    state.update(overrides)
    return state


def test_generate_doubao_video_confirms_the_parameter_read_back_then_renders() -> None:
    """Doubao answers with a parameter read-back and waits.

    A poll-only loop never sees a video card, so it used to burn the whole
    timeout and report "timed out without a playable video" even though nothing
    had been submitted for rendering.
    """
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[
            # 1st sighting: the tracker waits for the turn to settle.
            _video_state(1, [_CONFIRMATION_REPLY]),
            # Settled -> answer with a confirmation turn.
            _video_state(1, [_CONFIRMATION_REPLY]),
            # Render accepted.
            _video_state(2, [_SUBMITTED_REPLY]),
            _video_state(2, [_SUBMITTED_REPLY], videoCoverCount=1),
            _video_state(
                2,
                [_SUBMITTED_REPLY],
                videoCoverCount=1,
                videoPlayers=[
                    {
                        "src": "https://v11-vdl.doubao.com/video/tos/cn/abc/?mime_type=video_mp4",
                        "width": 1280,
                        "height": 720,
                        "duration": 10.08,
                    }
                ],
            ),
        ],
    )

    asset = generate_doubao_video(
        page,
        prompt="一只小猫在草地上奔跑",
        model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
        timeout_seconds=5,
        player_timeout_seconds=1,
    )

    assert asset.url.endswith("mime_type=video_mp4")
    # One send for the prompt, one for the confirmation.
    assert page.send_clicks == 2
    assert "确认，请按上述参数开始生成视频。" in page.filled


def test_generate_doubao_video_ignores_a_preamble_that_says_confirm() -> None:
    """Bare "确认" in a normal preamble must not trigger a second render.

    "本次生成将消耗每日免费额度，请确认后继续等待" means "going ahead", not
    "waiting for you"; answering it would submit a duplicate video request.
    """
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[
            _video_state(1, ["本次生成将消耗每日免费额度，请确认后继续等待。"]),
            _video_state(1, ["本次生成将消耗每日免费额度，请确认后继续等待。"]),
            _video_state(
                1,
                ["本次生成将消耗每日免费额度，请确认后继续等待。"],
                videoPlayers=[
                    {"src": "https://v11-vdl.doubao.com/x?mime_type=video_mp4"}
                ],
            ),
        ],
    )

    generate_doubao_video(
        page,
        prompt="一只小猫",
        model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
        timeout_seconds=5,
        player_timeout_seconds=1,
    )

    assert page.send_clicks == 1
    # The prompt itself is filled (via the fake's fallback path); what must not
    # happen is a confirmation turn.
    assert [value for value in page.filled if value.startswith("确认")] == []


def test_generate_doubao_video_answers_a_given_confirmation_turn_only_once() -> None:
    """A stale `texts` snapshot must not submit the confirmation repeatedly."""
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[
            _video_state(1, [_CONFIRMATION_REPLY]),
            _video_state(1, [_CONFIRMATION_REPLY]),
            # The answer lands, but no new turn has streamed in yet.
            _video_state(1, [_CONFIRMATION_REPLY]),
            _video_state(2, [_SUBMITTED_REPLY], videoPlayers=[{"src": "https://x/y.mp4"}]),
        ],
    )

    generate_doubao_video(
        page,
        prompt="一只小猫",
        model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
        timeout_seconds=5,
        player_timeout_seconds=1,
    )

    assert page.send_clicks == 2


def test_generate_doubao_video_fails_loudly_when_confirmations_never_converge() -> None:
    """Cap the loop so a chatty model cannot submit render after render."""
    def ask(index: int) -> str:
        return f"第 {index} 次询问：请先确认以下参数，确认后我再开始生成视频。"

    states = []
    for round_index in range(1, 5):
        states.append(_video_state(round_index, [ask(round_index)]))
        states.append(_video_state(round_index, [ask(round_index)]))

    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=states,
    )

    with pytest.raises(RuntimeError, match="keeps asking for confirmation"):
        generate_doubao_video(
            page,
            prompt="一只小猫",
            model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
            timeout_seconds=5,
            player_timeout_seconds=1,
        )

    # prompt + exactly _MAX_CONFIRMATION_ROUNDS answers, never more.
    assert page.send_clicks == 1 + 3


def test_generate_doubao_images_uses_the_render_sse_after_a_confirmation_turn() -> None:
    """The render's SSE is not necessarily the first completion anymore."""
    confirmation_sse = _sse(_creation_event(status=1))
    render_sse = _sse(
        _creation_event(
            status=1,
            raw_url="",
        ),
        _creation_event(
            status=2,
            raw_url="https://cdn.example/abc~image_raw.png",
            thumb_url="https://cdn.example/abc~thumb.png",
        ),
    )
    page = FakePage(
        model_label="Seedream 5.0 Flash",
        placeholder="描述你想要的图片",
        media_states=[
            _video_state(1, ["请先确认以下参数，确认后我再开始生成图片。"]),
            _video_state(1, ["请先确认以下参数，确认后我再开始生成图片。"]),
            _video_state(2, ["已生成。"]),
            _video_state(
                2,
                ["已生成。"],
                imageUrls=["https://cdn.example/abc~thumb.png"],
                imageFinished=True,
            ),
            _video_state(
                2,
                ["已生成。"],
                imageUrls=["https://cdn.example/abc~thumb.png"],
                imageFinished=True,
            ),
        ],
        sse_body=[confirmation_sse, render_sse],
    )

    assets = generate_doubao_images(
        page,
        prompt="一只戴墨镜的猫",
        model=resolve_media_model("doubao/seedream-5.0-flash", kind="image"),
        count=1,
        timeout_seconds=30,
        per_image_timeout_seconds=30,
    )

    assert [asset.url for asset in assets] == ["https://cdn.example/abc~image_raw.png"]
    assert page.send_clicks == 2
    assert "确认，请按上述参数开始生成图片。" in page.filled


def test_confirmation_tracker_waits_for_a_stable_turn() -> None:
    """Answering a half-streamed reply would interleave two assistant turns."""
    from opentoken.providers.doubao_media import _ConfirmationTracker

    sent: list[str] = []

    class Page:
        def evaluate(self, script, arg=None):
            return True

    tracker = _ConfirmationTracker(kind="视频")
    partial = _video_state(1, ["我将为你生成一只小猫，请先确认以下"])
    complete = _video_state(1, [_CONFIRMATION_REPLY])

    import opentoken.providers.doubao_media as module

    original_fill = module._doubao_fill_composer
    original_click = module._doubao_click_send_button
    module._doubao_fill_composer = lambda page, message: sent.append(message)
    module._doubao_click_send_button = lambda page: sent.append("<send>")
    try:
        # Growing text: never answered.
        assert tracker.answer(Page(), partial, before_turn_count=0) is False
        assert sent == []
        # Same turn, more text: still not stable.
        assert tracker.answer(Page(), complete, before_turn_count=0) is False
        assert sent == []
        # Unchanged across polls: answer exactly once.
        assert tracker.answer(Page(), complete, before_turn_count=0) is True
        assert sent == ["确认，请按上述参数开始生成视频。", "<send>"]
        assert tracker.answer(Page(), complete, before_turn_count=0) is False
        assert len(sent) == 2
    finally:
        module._doubao_fill_composer = original_fill
        module._doubao_click_send_button = original_click


def test_confirmation_tracker_ignores_turns_before_the_baseline() -> None:
    from opentoken.providers.doubao_media import _ConfirmationTracker

    tracker = _ConfirmationTracker(kind="视频")

    # Old confirmation text that predates this request must not be answered.
    state = _video_state(3, [_CONFIRMATION_REPLY])
    assert tracker.answer(None, state, before_turn_count=3) is False


# --------------------------------------------------------------------------- #
# Quota exhaustion: abort immediately instead of waiting out the timeout
# --------------------------------------------------------------------------- #
# Verbatim shape of the reply Doubao sends when the daily free tier runs out.
_QUOTA_REPLY = (
    "今日视频生成免费次数用完了，你明天再来找我继续吧。开通豆包专业版标准套餐即可使用"
    "高级创作模型，还可按需购买创作额度包，获得更多创作次数，免等待继续创作。"
)


def test_generate_doubao_video_aborts_at_once_when_the_quota_is_gone() -> None:
    """Quota exhaustion is plain assistant prose, so a poll-only loop used to
    burn the full 600s `timeout` and then report a render timeout."""
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[
            _video_state(1, ["视频生成中"]),
            _video_state(1, [_QUOTA_REPLY]),
            _video_state(1, [_QUOTA_REPLY]),
        ],
    )

    started = time.monotonic()
    with pytest.raises(ProviderRateLimitError, match="quota exhausted"):
        generate_doubao_video(
            page,
            prompt="一只小猫",
            model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
            # A large budget proves the abort did not come from the deadline.
            timeout_seconds=600,
            player_timeout_seconds=600,
        )
    assert time.monotonic() - started < 5

    # No confirmation turn was sent, and the prompt went out exactly once.
    assert page.send_clicks == 1
    assert [value for value in page.filled if value.startswith("确认")] == []


def test_generate_doubao_video_detects_quota_text_wrapped_by_the_ui() -> None:
    """A bolded heading splits the phrase across a line break, which innerText
    renders as a space; matching must not depend on exact spacing."""
    wrapped = "今日视频生成免费次数\n用完了，你明天再来找我继续吧。"
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[_video_state(1, [wrapped])],
    )

    with pytest.raises(ProviderRateLimitError):
        generate_doubao_video(
            page,
            prompt="一只小猫",
            model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
            timeout_seconds=600,
            player_timeout_seconds=600,
        )


def test_generate_doubao_video_does_not_treat_the_free_quota_preamble_as_exhausted() -> None:
    """The normal preamble mentions the free quota without being exhausted."""
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[
            _video_state(1, ["本次生成将消耗每日免费额度，预计等待 5 分钟。"]),
            _video_state(
                1,
                ["本次生成将消耗每日免费额度，预计等待 5 分钟。"],
                videoPlayers=[{"src": "https://x/y.mp4"}],
            ),
        ],
    )

    asset = generate_doubao_video(
        page,
        prompt="一只小猫",
        model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
        timeout_seconds=5,
        player_timeout_seconds=1,
    )

    assert asset.url == "https://x/y.mp4"


def test_generate_doubao_images_aborts_at_once_when_the_quota_is_gone() -> None:
    page = FakePage(
        model_label="Seedream 5.0 Flash",
        placeholder="描述你想要的图片",
        media_states=[
            _video_state(
                1,
                [
                    "今日图片生成免费次数用完了，你明天再来找我继续吧。开通豆包专业版标准套餐…"
                ],
            )
        ],
    )

    started = time.monotonic()
    with pytest.raises(ProviderRateLimitError, match="quota exhausted"):
        generate_doubao_images(
            page,
            prompt="一只戴墨镜的猫",
            model=resolve_media_model("doubao/seedream-5.0-flash", kind="image"),
            count=1,
            timeout_seconds=600,
            per_image_timeout_seconds=600,
        )
    assert time.monotonic() - started < 5


def test_media_failure_markers_cover_quota_and_violation_separately() -> None:
    """Quota must not be lumped in with content refusals: one is retryable
    tomorrow (429), the other is not (502)."""
    from opentoken.providers.doubao_media import (
        _DOUBAO_MEDIA_FAILURE_MARKERS,
        _DOUBAO_MEDIA_HARD_QUOTA_MARKERS,
        _find_marker,
    )

    assert _find_marker(_QUOTA_REPLY, _DOUBAO_MEDIA_HARD_QUOTA_MARKERS)
    assert not _find_marker(_QUOTA_REPLY, _DOUBAO_MEDIA_FAILURE_MARKERS)

    refusal = "该描述涉嫌违规，生成失败。"
    assert _find_marker(refusal, _DOUBAO_MEDIA_FAILURE_MARKERS)
    assert not _find_marker(refusal, _DOUBAO_MEDIA_HARD_QUOTA_MARKERS)


# Verbatim shape seen live when the daily free tier is gone but paid quota is
# available: Doubao asks whether to keep going on paid quota.
_PAID_QUOTA_REPLY = (
    "我将为你生成一只写实小猫在明媚阳光下的草地上轻快奔跑的 10 秒横屏视频。 "
    "今日视频生成免费额度已用完，本次将消耗付费额度。是否继续生成？ 确认生成"
)


def test_generate_doubao_video_clicks_the_paid_quota_confirm_button(monkeypatch) -> None:
    """The paid-quota offer is a button, so a chat message cannot clear it.

    Observed live: replying with text leaves the request stalled forever because
    only "确认生成" advances the turn.
    """
    monkeypatch.delenv("OPENTOKEN_DOUBAO_ALLOW_PAID_MEDIA", raising=False)
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[
            _video_state(1, [_PAID_QUOTA_REPLY]),
            # Button clicked; the render finally starts.
            _video_state(2, ["视频生成中"]),
            _video_state(
                2, ["视频生成中"], videoPlayers=[{"src": "https://x/y.mp4"}]
            ),
        ],
    )
    page.consent_buttons.append(
        {"label": "确认生成", "turnText": _PAID_QUOTA_REPLY, "inReceiveMessage": True}
    )

    asset = generate_doubao_video(
        page,
        prompt="一只小猫",
        model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
        timeout_seconds=10,
        player_timeout_seconds=5,
    )

    assert asset.url == "https://x/y.mp4"
    assert page.consent_clicks == 1
    # The button did the work; no extra chat turn was submitted.
    assert page.send_clicks == 1


def test_generate_doubao_video_refuses_paid_quota_when_policy_disables_it(
    monkeypatch,
) -> None:
    """Spending money must be switchable off for callers that can't audit it."""
    monkeypatch.setenv("OPENTOKEN_DOUBAO_ALLOW_PAID_MEDIA", "0")
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[
            _video_state(1, [_PAID_QUOTA_REPLY]),
            _video_state(1, [_PAID_QUOTA_REPLY]),
        ],
    )
    page.consent_buttons.append(
        {"label": "确认生成", "turnText": _PAID_QUOTA_REPLY, "inReceiveMessage": True}
    )

    started = time.monotonic()
    with pytest.raises(ProviderRateLimitError, match="paid quota"):
        generate_doubao_video(
            page,
            prompt="一只小猫",
            model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
            timeout_seconds=600,
            player_timeout_seconds=600,
        )
    assert time.monotonic() - started < 5
    # Crucially: the button was never clicked, so no money was spent.
    assert page.consent_clicks == 0


def test_paid_quota_reply_is_not_confused_with_the_plain_confirmation_flow() -> None:
    """Both ask a question, so the marker sets must not overlap."""
    from opentoken.providers.doubao_media import (
        _DOUBAO_CONFIRMATION_MARKERS,
        _DOUBAO_MEDIA_HARD_QUOTA_MARKERS,
        _DOUBAO_MEDIA_PAID_QUOTA_MARKERS,
        _find_marker,
    )

    assert _find_marker(_PAID_QUOTA_REPLY, _DOUBAO_MEDIA_PAID_QUOTA_MARKERS)
    assert not _find_marker(_PAID_QUOTA_REPLY, _DOUBAO_CONFIRMATION_MARKERS)
    # The paid wording also contains "免费额度已用完", which the hard set matches
    # by design (an older build says exactly that with no paid option). Precedence
    # in _raise_for_media_failure is what keeps the two apart: paid is checked
    # first, so the money question never gets reported as a plain exhaustion.
    assert _find_marker(_PAID_QUOTA_REPLY, _DOUBAO_MEDIA_HARD_QUOTA_MARKERS)

    # The hard "come back tomorrow" wording is the opposite: no paid option.
    assert not _find_marker(_QUOTA_REPLY, _DOUBAO_MEDIA_PAID_QUOTA_MARKERS)
    assert _find_marker(_QUOTA_REPLY, _DOUBAO_MEDIA_HARD_QUOTA_MARKERS)


def test_generate_doubao_video_clicks_the_compliance_notice_and_resends() -> None:
    """The compliance gate ("素材均已获充分授权") appears with no assistant turn.

    It can land before the prompt is submitted, so the click only dismisses the
    notice — the prompt has to be sent again or the render never starts.
    """
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[
            # Gate is up, nothing submitted yet: no new assistant turn.
            _video_state(0, []),
            # After the retry, Doubao accepts and renders.
            _video_state(1, ["视频生成中"]),
            _video_state(1, ["视频生成中"], videoPlayers=[{"src": "https://x/y.mp4"}]),
        ],
    )
    page.consent_buttons.append(
        {"label": "确认生成", "turnText": "", "inReceiveMessage": False}
    )

    asset = generate_doubao_video(
        page,
        prompt="一只小猫",
        model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
        timeout_seconds=10,
        player_timeout_seconds=5,
    )

    assert asset.url == "https://x/y.mp4"
    assert page.consent_clicks == 1
    # Initial send + the resend after the gate was dismissed.
    assert page.send_clicks == 2


def test_generate_doubao_video_does_not_resend_when_the_gate_comes_after_sending() -> None:
    """If a turn already exists, the prompt did go out — resending would submit a
    duplicate render (and a duplicate charge)."""
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[
            _video_state(1, ["视频生成中"]),
            _video_state(1, ["视频生成中"], videoPlayers=[{"src": "https://x/y.mp4"}]),
        ],
    )
    page.consent_buttons.append(
        {"label": "确认生成", "turnText": "视频生成中", "inReceiveMessage": True}
    )

    generate_doubao_video(
        page,
        prompt="一只小猫",
        model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
        timeout_seconds=10,
        player_timeout_seconds=5,
    )

    assert page.consent_clicks == 1
    assert page.send_clicks == 1


def test_a_stale_consent_button_is_clicked_only_once() -> None:
    """If the button lingers in the DOM after the click, don't click it again.

    A second click on a still-rendered gate could submit a duplicate render (and,
    on the paid gate, charge twice), so the same turn is only ever answered once.
    """
    from opentoken.providers.doubao_media import _ConfirmationTracker

    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[_video_state(0, [])],
    )
    page.consent_buttons = [
        {"label": "确认生成", "turnText": "", "inReceiveMessage": False}
        for _ in range(10)
    ]

    tracker = _ConfirmationTracker(kind="视频")
    state = _video_state(0, [])
    outcomes = [tracker.handle(page, state, before_turn_count=0) for _ in range(4)]

    assert outcomes.count("clicked") == 1
    assert page.consent_clicks == 1


def test_consent_clicks_are_capped_across_distinct_gates() -> None:
    """Compliance + paid quota is two gates; a third is a UI loop, so stop."""
    from opentoken.providers.doubao_media import _ConfirmationTracker

    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[_video_state(0, [])],
    )
    page.consent_buttons = [
        {"label": "确认生成", "turnText": f"第{index}次要求确认", "inReceiveMessage": True}
        for index in range(1, 6)
    ]

    tracker = _ConfirmationTracker(kind="视频")
    outcomes = [
        tracker.handle(
            page,
            _video_state(index, [f"第{index}次要求确认"]),
            before_turn_count=0,
        )
        for index in range(1, 6)
    ]

    assert outcomes.count("clicked") == 3  # _MAX_CONSENT_CLICKS
    assert page.consent_clicks == 3


def test_media_state_without_a_consent_button_is_a_no_op() -> None:
    """No button on screen must be reported as such, not as a click."""
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[_video_state(1, ["视频生成中"])],
    )

    from opentoken.providers.doubao_media import _click_media_consent_button

    result = _click_media_consent_button(page)

    assert result["clicked"] is False
    assert page.consent_clicks == 0


def test_consent_button_selector_only_matches_short_confirm_labels() -> None:
    """Guard the regex against ordinary prose that merely contains the words.

    If "确认生成" matched loosely, the gateway could click a normal send button or
    a link in a message body.
    """
    import re

    from opentoken.providers.doubao_media import _MEDIA_CONSENT_BUTTON_JS

    pattern = re.search(r"/\^\((.*?)\)\$/", _MEDIA_CONSENT_BUTTON_JS)
    assert pattern is not None, "the JS label regex moved; update this test"
    allowed = re.compile("^(" + pattern.group(1) + ")$")

    assert allowed.match("确认生成")
    assert allowed.match("继续生成")
    assert allowed.match("确认继续")
    assert not allowed.match("确认")
    assert not allowed.match("确认生成视频并发送给我")
    assert not allowed.match("生成")


# Verbatim from a live render: paid quota is *already* in use, and there is no
# question and no button. Aborting here killed a render that was in flight.
_PAID_QUOTA_NOTICE = (
    "本次使用 Seedance 2.0 Mini 生成，预计等待 5 分钟。视频生成好后，我会主动发送给你。"
    "今日视频生成免费额度已用完，本次将消耗付费额度，并使用优先生成通道。"
)


def test_paid_quota_notice_does_not_abort_an_in_flight_render() -> None:
    """The notice contains "免费额度已用完" (a hard-quota marker) but must not stop."""
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[
            _video_state(1, [_PAID_QUOTA_NOTICE]),
            _video_state(
                2, [_PAID_QUOTA_NOTICE], videoPlayers=[{"src": "https://x/y.mp4"}]
            ),
        ],
    )

    asset = generate_doubao_video(
        page,
        prompt="一只小猫",
        model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
        timeout_seconds=10,
        player_timeout_seconds=5,
    )

    assert asset.url == "https://x/y.mp4"
    assert page.send_clicks == 1
    assert page.consent_clicks == 0


def test_paid_quota_offer_and_notice_are_told_apart() -> None:
    from opentoken.providers.doubao_media import (
        _DOUBAO_MEDIA_PAID_OFFER_MARKERS,
        _DOUBAO_MEDIA_PAID_QUOTA_MARKERS,
        _find_marker,
    )

    assert _find_marker(_PAID_QUOTA_NOTICE, _DOUBAO_MEDIA_PAID_QUOTA_MARKERS)
    assert not _find_marker(_PAID_QUOTA_NOTICE, _DOUBAO_MEDIA_PAID_OFFER_MARKERS)

    assert _find_marker(_PAID_QUOTA_REPLY, _DOUBAO_MEDIA_PAID_QUOTA_MARKERS)
    assert _find_marker(_PAID_QUOTA_REPLY, _DOUBAO_MEDIA_PAID_OFFER_MARKERS)


# --------------------------------------------------------------------------- #
# Video options (aspect ratio + duration) and reference images
# --------------------------------------------------------------------------- #
def _video_render_page(**overrides) -> FakePage:
    page = FakePage(
        model_label="Seedance 2.0 Mini",
        placeholder="描述你想要的视频",
        media_states=[
            _video_state(1, ["视频生成中"]),
            _video_state(1, ["视频生成中"], videoPlayers=[{"src": "https://x/y.mp4"}]),
        ],
        **overrides,
    )
    return page


def test_generate_doubao_video_sets_the_aspect_ratio_and_duration() -> None:
    """Both controls are driven through the options panel, then verified."""
    page = _video_render_page()

    generate_doubao_video(
        page,
        prompt="一只小猫",
        model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
        timeout_seconds=10,
        player_timeout_seconds=5,
        seconds=12,
        size="720x1280",
    )

    assert page.video_ratio == "9:16"
    assert page.video_seconds == 12
    assert page.slider_value == 12 - 4


def test_generate_doubao_video_leaves_options_alone_when_not_requested() -> None:
    page = _video_render_page()

    generate_doubao_video(
        page,
        prompt="一只小猫",
        model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
        timeout_seconds=10,
        player_timeout_seconds=5,
    )

    assert page.video_ratio == "自动"
    assert page.video_seconds == 10
    assert page.panel_open is False


def test_generate_doubao_video_uploads_the_reference_image_before_sending() -> None:
    """The attachment belongs to the turn the prompt starts, so it must be in the
    composer before send — and the render must still be awaited normally."""
    page = _video_render_page()
    reference = Path("/tmp/opentoken-reference.png")

    generate_doubao_video(
        page,
        prompt="让这只猫跑起来",
        model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
        timeout_seconds=10,
        player_timeout_seconds=5,
        seconds=6,
        size="1280x720",
        reference_images=[reference],
    )

    assert page.uploaded == [[str(reference)]]
    assert page.send_clicks == 1
    assert page.video_ratio == "16:9"
    assert page.video_seconds == 6


def test_generate_doubao_video_clears_stale_attachments_before_uploading() -> None:
    """A leftover image from a previous turn would otherwise join the reference set."""
    page = _video_render_page()
    page.attachments = 2

    generate_doubao_video(
        page,
        prompt="让这只猫跑起来",
        model=resolve_media_model("doubao/seedance-2.0-mini", kind="video"),
        timeout_seconds=10,
        player_timeout_seconds=5,
        reference_images=[Path("/tmp/opentoken-reference.png")],
    )

    assert page.attachments == 1


def test_generate_doubao_video_rejects_reference_images_for_unsupported_models() -> None:
    """Seedance 2.0 Fast says in its own UI that it cannot take reference images."""
    page = _video_render_page()

    with pytest.raises(RuntimeError, match="cannot take a reference image"):
        generate_doubao_video(
            page,
            prompt="让这只猫跑起来",
            model=resolve_media_model("doubao/seedance-2.0-fast", kind="video"),
            timeout_seconds=10,
            player_timeout_seconds=5,
            reference_images=[Path("/tmp/opentoken-reference.png")],
        )
    assert page.uploaded == []


def test_video_ratio_mapping_covers_every_offered_option() -> None:
    from opentoken.models.media import VIDEO_RATIOS, video_ratio_for_size

    assert video_ratio_for_size("1280x720") == "16:9"
    assert video_ratio_for_size("720x1280") == "9:16"
    assert video_ratio_for_size("1024x1024") == "1:1"
    assert video_ratio_for_size("1024x768") == "4:3"
    assert video_ratio_for_size("768x1024") == "3:4"
    # 21:9 is the widest option Doubao offers.
    assert video_ratio_for_size("2520x1080") == "21:9"
    assert set(VIDEO_RATIOS) == {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}


def test_duration_is_driven_through_the_panel_scoped_slider_only() -> None:
    """Regression (vidgen-87b123b4): the slider used to be looked up page-wide.

    `page.locator('[role="slider"]').first` matches the transcript's video seek
    bar as soon as one render exists, so the arrow keys moved the wrong control
    while the post-check read `aria-valuenow` back from that same wrong element
    and reported success — leaving the composer at its default 9s.
    """
    page = _video_render_page()

    generate_doubao_video(
        page,
        prompt="一只小猫",
        model=resolve_media_model("doubao/seedance-2.5", kind="video"),
        timeout_seconds=10,
        player_timeout_seconds=5,
        seconds=5,
    )

    assert page.slider_locator_uses == 0, "the duration slider must be panel-scoped"
    assert page.video_seconds == 5


def test_a_duration_mapping_mismatch_is_reconciled_against_the_chip() -> None:
    """The aria→seconds mapping is upstream's business, so the chip decides.

    Here the panel numbering is off by one: aiming at `5 - 4` lands on 6s. The
    old code trusted `aria-valuenow` and failed ("composer shows 9s after
    setting 5s") instead of stepping once more.
    """
    page = _video_render_page()
    page.slider_seconds_override = 5  # chip = 5 + aria-valuenow

    generate_doubao_video(
        page,
        prompt="一只小猫",
        model=resolve_media_model("doubao/seedance-2.5", kind="video"),
        timeout_seconds=10,
        player_timeout_seconds=5,
        seconds=5,
    )

    assert page.video_seconds == 5
    assert page.video_ratio == "自动"


def test_an_unreachable_duration_fails_instead_of_rendering_the_wrong_length() -> None:
    """If stepping the control never changes the chip, say so and stop.

    Silently submitting Doubao's 9s default would bill a render the caller did
    not ask for, so this must be an error — but a precise one.
    """
    page = _video_render_page()
    page.video_seconds = 9
    page.slider_seconds = lambda aria_value: 9  # chip is stuck on 9s

    with pytest.raises(RuntimeError, match="does not seem selectable"):
        generate_doubao_video(
            page,
            prompt="一只小猫",
            model=resolve_media_model("doubao/seedance-2.5", kind="video"),
            timeout_seconds=10,
            player_timeout_seconds=5,
            seconds=5,
        )


def test_a_panel_with_discrete_duration_buttons_is_clicked_by_label() -> None:
    """Newer panels drop the slider for '5s'/'9s' buttons; no arrows involved."""
    page = _video_render_page()
    page.panel_slider = False
    page.panel_durations = ["5s", "9s"]

    generate_doubao_video(
        page,
        prompt="一只小猫",
        model=resolve_media_model("doubao/seedance-2.5", kind="video"),
        timeout_seconds=10,
        player_timeout_seconds=5,
        seconds=5,
    )

    assert page.video_seconds == 5
    assert ("text", "5s") in page.clicks
    assert page.slider_locator_uses == 0
