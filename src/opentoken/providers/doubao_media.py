"""Doubao (Seedream / Seedance) image and video generation over the web UI.

Doubao's media skills are only reachable from the chat composer: picking the
``图像生成`` / ``视频生成`` chip swaps the composer into a media prompt box, and
the page then fires its own signed ``/chat/completion`` request. There is no
public image/video endpoint to call instead — an unsigned in-page fetch hits the
same anti-bot wall as chat — so this module drives the UI exactly like
``_dom_send_and_wait_doubao`` does for text.

Two delivery shapes show up here:

* **Images** come back inside the completion SSE we already capture. Each
  ``creation_block`` carries several signed variants of the same render; the
  ``image_ori_raw`` one is the full-size, watermark-free PNG while the DOM only
  renders a ~1K thumbnail, so the SSE body is the primary source and the DOM is
  the fallback for when a build stops emitting ``creation_block``.
* **Videos** finish *after* the completion SSE closes: Doubao replies "视频生成
  已提交…" and pushes the render as a separate chat message minutes later. We
  poll the transcript for the new ``block-video`` card, click its player (the
  ``<video src>`` only mounts once the player is opened), and read the signed
  CDN URL from the element.

Everything here is best-effort against a UI that changes without notice, so
selectors are matched by role/label/class-prefix and every wait has a deadline
that raises a message naming what never showed up.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from opentoken.models.media import MediaModel
from opentoken.providers.base import ProviderRateLimitError
from opentoken.providers.camoufox_clients import (
    _doubao_click_send_button,
    _doubao_fill_composer,
    _detach_page_listener,
)
from opentoken.tasks import active_recorder, record_event, record_section

# Same logger name as camoufox_clients so media logs land next to the chat ones
# under the request_id of the HTTP call that triggered the render.
logger = logging.getLogger("opentoken.providers.browser")

DOUBAO_IMAGE_SKILL_LABEL = "图像生成"
DOUBAO_VIDEO_SKILL_LABEL = "视频生成"

_DOUBAO_IMAGE_COMPOSER_PLACEHOLDER = "描述你想要的图片"
_DOUBAO_VIDEO_COMPOSER_PLACEHOLDER = "描述你想要的视频"

# Doubao refuses/blocks some prompts; surface those immediately instead of
# burning the whole video timeout. Deliberately specific so the "本次生成将消耗
# 每日免费额度" preamble never trips it.
_DOUBAO_MEDIA_FAILURE_MARKERS = (
    "生成失败",
    "无法生成",
    "生成出错",
    "不符合平台",
    "涉嫌违规",
    "已停止生成",
)

# Quota exhaustion is reported as ordinary assistant prose, not an error status,
# so a poll-only loop would sit there until the deadline and then blame the
# render. Doubao words it per modality and adds an upsell, e.g.
#   "今日视频生成免费次数用完了，你明天再来找我继续吧。开通豆包专业版标准套餐
#    即可使用高级创作模型，还可按需购买创作额度包…"
# These are kept separate from the failure markers because they map to 429
# rate_limit_error (a retry tomorrow / different account fixes it), not 502.
#
# Only phrases that appear exclusively in the exhausted message belong here: an
# upsell mention like "额度包" can also trail a *successful* render, and failing
# on those would discard a finished video.
#
# NOTE: "免费额度已用完" also appears in the paid-quota question below, which is
# matched first. Order in `_raise_for_media_failure` is what separates them.
_DOUBAO_MEDIA_HARD_QUOTA_MARKERS = (
    "免费次数用完",
    "免费次数已用完",
    "免费额度已用完",
    "额度已用完",
    "次数用完了",
    "明天再来找我",
)

# The other quota reply mentions *paid* quota instead of refusing outright. It
# comes in two shapes, and only one of them blocks anything (both seen live):
#
#   offer    "今日视频生成免费额度已用完，本次将消耗付费额度。是否继续生成？"
#            + a "确认生成" button — nothing renders until that is clicked.
#   notice   "今日视频生成免费额度已用完，本次将消耗付费额度，并使用优先生成通道。"
#            — no question, no button: Doubao already queued the render on the
#            paid channel. Treating this as a stop aborted renders that were
#            already running.
#
# So the marker set below only answers "is paid quota involved?"; the offer set
# answers "is it waiting on me?".
_DOUBAO_MEDIA_PAID_QUOTA_MARKERS = (
    "消耗付费额度",
    "使用付费额度",
    "将消耗付费",
)

_DOUBAO_MEDIA_PAID_OFFER_MARKERS = (
    "是否继续生成",
    "是否继续创作",
    "是否继续",
    "确认生成",
)

# Doubao often answers a video request with a parameter read-back ("我将为你生成…
# 请先确认以下参数 … 确认后我再开始生成视频") and then WAITS for another turn. The
# transcript gets no video card, so a poll-only loop burns the whole timeout and
# reports "timed out without a playable video" even though the render was never
# started. These markers identify that pause-for-confirmation reply.
#
# Every marker is a multi-character phrase on purpose: bare "确认" appears in
# ordinary preambles ("本次生成将消耗额度，请确认") that already mean "going ahead",
# and answering those would submit a second, duplicate render.
_DOUBAO_CONFIRMATION_MARKERS = (
    "确认后我再开始",
    "确认后再开始",
    "确认后开始生成",
    "确认后我将",
    "请先确认以下",
    "请确认以下参数",
    "确认以下参数",
    "参数确认",
    "确认无误后",
    "请回复确认",
    "是否确认",
    "需要你确认",
    "等你确认",
)

# A confirmation loop that never converges (e.g. Doubao keeps asking) must fail
# loudly rather than submit render after render until the deadline.
_MAX_CONFIRMATION_ROUNDS = 3

# Cap on "确认生成" gate clicks per render. Two gates (compliance + paid quota)
# is the realistic maximum; the cap stops a UI glitch from clicking forever.
_MAX_CONSENT_CLICKS = 3

# How many `/chat/completion` SSE bodies to keep while waiting for an image. A
# confirmation turn consumes one, so the render's body is not necessarily first.
_MAX_CAPTURED_COMPLETIONS = 4

_ROLE_CANDIDATES = ("menuitem", "option", "menuitemradio")


@dataclass(frozen=True)
class MediaAsset:
    """One rendered image or video, as reported by Doubao's signed CDN URLs."""

    url: str
    media_type: str
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    revised_prompt: str | None = None


def media_ui_label(model: MediaModel) -> str:
    return model.label


# --------------------------------------------------------------------------- #
# Composer / skill / model-picker plumbing
# --------------------------------------------------------------------------- #
def _composer_placeholder(page) -> str:
    try:
        return str(
            page.evaluate(
                """
                () => {
                  const node = document.querySelector(
                    'div[contenteditable="true"].ProseMirror p[data-placeholder], ' +
                    '[data-testid="chat_input_input"] p[data-placeholder], ' +
                    'textarea[placeholder]'
                  );
                  if (!node) return '';
                  return node.getAttribute('data-placeholder') || node.getAttribute('placeholder') || '';
                }
                """
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _wait_for_placeholder(page, placeholder: str, *, timeout_seconds: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        current = _composer_placeholder(page)
        if placeholder in current:
            return
        page.wait_for_timeout(300)
    raise RuntimeError(
        f"Doubao did not switch its composer to '{placeholder}'. "
        f"Current placeholder: '{_composer_placeholder(page) or '<none>'}'."
    )


def _wait_for_skill_bar(page, *, timeout_seconds: float = 30.0) -> bool:
    """Wait until the skill chips are rendered.

    The bar mounts asynchronously after the chat SPA hydrates; clicking before
    that raised a misleading "Doubao skill '视频生成' is not available" (observed
    live right after a fresh session on about:blank).
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if page.locator('button[data-testid^="skill_bar_button_"]').count():
                return True
        except Exception:
            pass
        page.wait_for_timeout(500)
    return False


def _click_skill_chip(page, label: str) -> None:
    if not _wait_for_skill_bar(page):
        raise RuntimeError(
            "Doubao's skill bar never rendered, so the media skills are "
            "unreachable. The page may still be loading or the session expired."
        )
    chips = page.locator('button[data-testid^="skill_bar_button_"]').filter(
        has_text=re.compile(re.escape(label))
    )
    try:
        chips.first.click(timeout=6000)
        return
    except Exception:
        pass
    # The skill bar collapses the rarely-used chips behind "更多"; open it and
    # pick the entry from that menu.
    try:
        page.locator('[data-testid="skill_bar_button_more"]').first.click(timeout=6000)
        page.wait_for_timeout(500)
        for role in _ROLE_CANDIDATES:
            entry = page.get_by_role(role).filter(has_text=re.compile(re.escape(label)))
            if entry.count():
                entry.first.click(timeout=5000)
                return
    except Exception as exc:
        raise RuntimeError(
            f"Doubao skill '{label}' is not available in the composer."
        ) from exc
    raise RuntimeError(f"Doubao skill '{label}' is not available in the composer.")


def _enter_skill(page, *, label: str, placeholder: str) -> None:
    """Switch the composer into a media skill.

    Idempotent: when the composer is already in that mode (e.g. the previous
    render left it there, or a retry re-enters), the skill bar is collapsed and
    clicking a chip is both unnecessary and unreliable.
    """
    if placeholder in _composer_placeholder(page):
        return
    _click_skill_chip(page, label)
    _wait_for_placeholder(page, placeholder)


def _label_pattern(label: str) -> re.Pattern[str]:
    """Anchor `label` but don't let a shorter label swallow its longer siblings.

    The menu renders "Seedance 2.0" and "Seedance 2.0 Fast"; a naive prefix match
    would make "Seedance 2.0" select "Seedance 2.0 Fast" (it is the first row
    containing the text).
    """
    from opentoken.models.media import MEDIA_MODELS

    all_labels = [model.label for model in MEDIA_MODELS]
    longer = [
        other for other in all_labels if other != label and other.startswith(label)
    ]
    if not longer:
        return re.compile(r"^" + re.escape(label))
    suffixes = "|".join(re.escape(other[len(label) :].strip()) for other in longer)
    return re.compile(r"^" + re.escape(label) + r"(?!\s*(?:" + suffixes + r"))")


def _current_media_model_label(page) -> str:
    try:
        text = page.locator("#input-engine-container button").filter(has_text="模型").first.inner_text(
            timeout=3000
        )
    except Exception:
        return ""
    collapsed = " ".join(str(text).split())
    for prefix in ("模型", "Model"):
        if collapsed.startswith(prefix):
            return collapsed[len(prefix) :].strip()
    return collapsed


def _select_media_model(page, label: str) -> None:
    pattern = _label_pattern(label)
    current = _current_media_model_label(page)
    if current and pattern.match(current):
        return
    trigger = page.locator("#input-engine-container button").filter(has_text="模型").first
    trigger.click(timeout=6000)
    page.wait_for_timeout(600)
    last_error: Exception | None = None
    for role in _ROLE_CANDIDATES:
        try:
            entry = page.get_by_role(role).filter(has_text=pattern).first
            entry.wait_for(timeout=4000)
            entry.click(timeout=5000)
            page.wait_for_timeout(400)
            return
        except Exception as exc:  # try the next role container
            last_error = exc
    raise RuntimeError(f"Doubao media model '{label}' not found in the model menu.") from last_error


# --------------------------------------------------------------------------- #
# Video options: aspect ratio + duration
# --------------------------------------------------------------------------- #
# The chip in the composer renders as "自动 · 10s" and opens a small panel with a
# row of ratio buttons and a 4s–15s slider. Neither control carries a
# data-testid, so they are addressed by their visible label (ratio) and by the
# ARIA slider contract (duration).
#
# Slider shape observed live: `span[role="slider"]` with aria-valuemin=0,
# aria-valuemax=11 and the label "4s"…"15s" at the two ends — i.e. the value is
# `4 + aria-valuenow`. Arrow keys move it one step, which is far more reliable
# than dragging a 338px track.
#
# Ratio buttons: `button.flex.h-56` inside `div.grid.gap-8`, label exactly
# "自动" / "3:4" / … — an exact-text match is required, because "9:16" is a
# substring of nothing but "自动" appears in other chips elsewhere on the page.
_VIDEO_PANEL_FINDER_JS = """
  const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const findPanel = () => {
    let panel = null;
    for (const node of document.querySelectorAll('div, section')) {
      const text = clean(node.innerText);
      if (!text.includes('比例') || !text.includes('时长')) continue;
      if (node.contains(document.querySelector('#input-engine-container'))) continue;
      // The innermost container holding both headings is the popover itself.
      if (!panel || text.length < clean(panel.innerText).length) panel = node;
    }
    return panel;
  };
"""

_VIDEO_PANEL_JS = (
    """
() => {"""
    + _VIDEO_PANEL_FINDER_JS
    + """
  const panel = findPanel();
  if (!panel) return { open: false };
  const sliders = Array.from(panel.querySelectorAll('[role="slider"]'));
  const slider = sliders[0] || null;
  return {
    open: true,
    ratios: Array.from(panel.querySelectorAll('button'))
      .map((node) => clean(node.innerText))
      .filter((label) => /^(自动|\\d{1,2}:\\d{1,2})$/.test(label)),
    durations: Array.from(panel.querySelectorAll('button, [role="radio"], [role="option"]'))
      .map((node) => clean(node.innerText))
      .filter((label) => /^\\d{1,2}s$/.test(label)),
    sliderCount: sliders.length,
    slider: slider
      ? {
          now: Number(slider.getAttribute('aria-valuenow')),
          min: Number(slider.getAttribute('aria-valuemin')),
          max: Number(slider.getAttribute('aria-valuemax')),
          label: slider.getAttribute('aria-label') || '',
          text: slider.getAttribute('aria-valuetext') || '',
        }
      : null,
  };
}
"""
)

# Focus and read the *panel's* slider in one round trip.
#
# `page.locator('[role="slider"]').first` is page-wide: as soon as the transcript
# holds a rendered video, its player seek bar is earlier in the DOM, so the arrow
# keys moved that control while the panel's duration stayed put — and the
# post-check read `aria-valuenow` from the same wrong element, so it "passed"
# (task vidgen-87b123b4: asked for 5s, composer kept showing 9s).
_FOCUS_DURATION_SLIDER_JS = (
    """
() => {"""
    + _VIDEO_PANEL_FINDER_JS
    + """
  const panel = findPanel();
  if (!panel) return { open: false, focused: false };
  const slider = panel.querySelector('[role="slider"]');
  if (!slider) return { open: true, focused: false, sliderCount: 0 };
  try { slider.scrollIntoView({ block: 'center' }); } catch (_error) {}
  try { slider.focus(); } catch (_error) {}
  return {
    open: true,
    focused: document.activeElement === slider,
    sliderCount: panel.querySelectorAll('[role="slider"]').length,
    now: Number(slider.getAttribute('aria-valuenow')),
    min: Number(slider.getAttribute('aria-valuemin')),
    max: Number(slider.getAttribute('aria-valuemax')),
    label: slider.getAttribute('aria-label') || '',
    text: slider.getAttribute('aria-valuetext') || '',
  };
}
"""
)

_READ_DURATION_SLIDER_JS = (
    """
() => {"""
    + _VIDEO_PANEL_FINDER_JS
    + """
  const panel = findPanel();
  if (!panel) return { open: false };
  const slider = panel.querySelector('[role="slider"]');
  if (!slider) return { open: true, slider: null };
  return {
    open: true,
    slider: {
      now: Number(slider.getAttribute('aria-valuenow')),
      min: Number(slider.getAttribute('aria-valuemin')),
      max: Number(slider.getAttribute('aria-valuemax')),
      label: slider.getAttribute('aria-label') || '',
      text: slider.getAttribute('aria-valuetext') || '',
    },
  };
}
"""
)

_DURATION_CHIP_PATTERN = re.compile(r"(?:自动|\d{1,2}:\d{1,2})\s*·\s*\d+s")
# Seconds out of the composer chip ("9:16 · 5s · 720P"). The chip is the only
# value Doubao itself reads back, so it — not aria-valuenow — is the truth.
_CHIP_SECONDS_PATTERN = re.compile(r"(?<![\d.:])(\d{1,2})s(?![\w.])")
# How many single-step nudges to spend reconciling the chip with the request.
_MAX_DURATION_NUDGES = 12


def _open_video_options_panel(page) -> dict[str, object]:
    """Open the ratio/duration popover and describe its controls."""
    state = page.evaluate(_VIDEO_PANEL_JS)
    if isinstance(state, dict) and state.get("open"):
        return state
    chip = page.locator("#input-engine-container button").filter(
        has_text=_DURATION_CHIP_PATTERN
    )
    if not chip.count():
        raise RuntimeError(
            "Doubao's video options chip ('自动 · 10s') is not in the composer, so "
            "the aspect ratio and duration cannot be set."
        )
    chip.first.click(timeout=8000)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        state = page.evaluate(_VIDEO_PANEL_JS)
        if isinstance(state, dict) and state.get("open"):
            return state
        page.wait_for_timeout(200)
    raise RuntimeError("Doubao's video options panel did not open.")


def _select_video_ratio(page, ratio: str) -> None:
    """Click the ratio button whose label is exactly `ratio`."""
    panel = _open_video_options_panel(page)
    available = [str(label) for label in panel.get("ratios") or []]
    if ratio not in available:
        raise RuntimeError(
            f"Doubao does not offer the '{ratio}' aspect ratio "
            f"(available: {', '.join(available) or '<none>'})."
        )
    # Scope to the panel text so a "16:9" elsewhere on the page cannot match.
    page.get_by_text(ratio, exact=True).last.click(timeout=6000)
    page.wait_for_timeout(400)


def _set_video_duration(page, seconds: int) -> None:
    """Set the duration to `seconds`, using the panel's *own* duration control.

    Two paths, because Doubao renders this control differently per model:

    * a slider (`role="slider"`, aria-valuenow 0..11 for 4s..15s), driven with
      the arrow keys — deterministic, unlike clicking a pixel on the track;
    * discrete buttons ("5s" / "9s" / …) on newer panels, which are simply
      clicked by label.

    Both paths then get reconciled against the composer chip by
    :func:`_reconcile_duration_with_chip`, because the aria mapping is a guess
    about upstream's numbering and the chip is what Doubao actually uses.
    """
    panel = _open_video_options_panel(page)
    record_event(
        "doubao_video_options_panel",
        stage="duration",
        ratios=panel.get("ratios"),
        durations=panel.get("durations"),
        slider_count=panel.get("sliderCount"),
        slider=panel.get("slider"),
    )
    slider_state = panel.get("slider")
    if isinstance(slider_state, dict):
        _drive_duration_slider(page, seconds)
        return
    labels = [str(label) for label in panel.get("durations") or []]
    if labels:
        _click_duration_option(page, seconds, labels)
        return
    raise RuntimeError(
        "Doubao's video duration control is not available: the options panel has "
        "neither a slider nor an 'Ns' button."
    )


def _click_duration_option(page, seconds: int, labels: list[str]) -> None:
    """Pick a discrete duration button from the panel."""
    wanted = f"{seconds}s"
    if wanted not in labels:
        raise RuntimeError(
            f"Doubao does not offer a {seconds}s duration for this model "
            f"(available: {', '.join(labels)})."
        )
    page.get_by_text(wanted, exact=True).last.click(timeout=6000)
    page.wait_for_timeout(400)


def _drive_duration_slider(page, seconds: int) -> None:
    """Move the panel-scoped slider to the position that should mean `seconds`."""
    focused = page.evaluate(_FOCUS_DURATION_SLIDER_JS)
    if not isinstance(focused, dict):
        raise RuntimeError("Could not read Doubao's video duration slider.")
    if not focused.get("open"):
        raise RuntimeError("Doubao's video options panel closed before the duration was set.")
    if not focused.get("focused"):
        # Without focus the arrow keys go nowhere, which is the failure mode that
        # produced "the slider says 5s but the chip says 9s".
        raise RuntimeError(
            "Doubao's video duration slider could not be focused, so the duration "
            f"cannot be changed (sliders in the panel: {focused.get('sliderCount')})."
        )
    low = int(focused.get("min") or 0)
    high = int(focused.get("max") or 0)
    if high <= low:
        raise RuntimeError(
            f"Doubao's duration slider reports an empty range ({low}..{high})."
        )
    target = seconds - _duration_offset(low, high)
    if not low <= target <= high:
        # The aria numbering is only a guess about upstream's mapping, so a
        # target outside it means the guess is wrong, not that the duration is
        # unsupported — clamp, and let the chip check below make the call.
        record_event(
            "doubao_video_duration_target_clamped",
            requested_seconds=seconds,
            aria_target=target,
            range=[low, high],
        )
        target = min(max(target, low), high)

    for _ in range(3):
        current_state = page.evaluate(_READ_DURATION_SLIDER_JS)
        slider = (current_state or {}).get("slider") if isinstance(current_state, dict) else None
        if not isinstance(slider, dict):
            raise RuntimeError("Doubao's video duration slider disappeared while being set.")
        current = int(slider.get("now"))
        delta = target - current
        if delta == 0:
            record_event(
                "doubao_video_duration_slider_set",
                requested_seconds=seconds,
                aria_target=target,
                aria_now=current,
                range=[low, high],
            )
            return
        key = "ArrowRight" if delta > 0 else "ArrowLeft"
        for _ in range(abs(delta)):
            page.keyboard.press(key)
            page.wait_for_timeout(60)
    final_state = page.evaluate(_READ_DURATION_SLIDER_JS)
    slider = (final_state or {}).get("slider") if isinstance(final_state, dict) else None
    current = int(slider.get("now")) if isinstance(slider, dict) else -1
    # `aria-valuenow` is upstream's own numbering and does not have to equal
    # seconds (a real 2.5 panel runs 0..26 with 9s at 6). Only the composer chip
    # is authoritative, so a missed aria target is recorded, not fatal.
    record_event(
        "doubao_video_duration_slider_short",
        requested_seconds=seconds,
        aria_target=target,
        aria_now=current,
        range=[low, high],
    )


def _chip_seconds(chip: str) -> int | None:
    match = _CHIP_SECONDS_PATTERN.search(chip or "")
    return int(match.group(1)) if match else None


def _reconcile_duration_with_chip(page, seconds: int) -> None:
    """Nudge the duration until the composer chip reports `seconds`.

    The aria-value → seconds mapping differs between panels, and a mismatch is
    only visible once the panel closes and the chip re-renders. Rather than
    failing on the first disagreement, step once in the needed direction and
    re-read; stop as soon as the chip agrees, or as soon as stepping no longer
    changes anything (the value is not reachable for this model).
    """
    chip = _duration_chip_text(page)
    current = _chip_seconds(chip)
    if current is None or current == seconds:
        return
    for attempt in range(1, _MAX_DURATION_NUDGES + 1):
        step_forward = current < seconds
        panel = _open_video_options_panel(page)
        if not isinstance(panel.get("slider"), dict):
            record_event(
                "doubao_video_duration_nudge_unsupported",
                chip=chip,
                requested_seconds=seconds,
            )
            return
        page.evaluate(_FOCUS_DURATION_SLIDER_JS)
        page.keyboard.press("ArrowRight" if step_forward else "ArrowLeft")
        page.wait_for_timeout(120)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        chip_after = _duration_chip_text(page)
        reached = _chip_seconds(chip_after)
        record_event(
            "doubao_video_duration_nudge",
            attempt=attempt,
            chip_before=chip,
            chip_after=chip_after,
            requested_seconds=seconds,
        )
        if reached == seconds:
            return
        if reached == current:
            # The control is at its limit or the step is not 1s: stop instead of
            # burning the whole nudge budget on a value this model cannot show.
            raise RuntimeError(
                f"Doubao's composer keeps showing '{chip_after}' while {seconds}s was "
                f"requested; {seconds}s does not seem selectable for this model."
            )
        current = reached if reached is not None else current


def _duration_offset(low: int, high: int) -> int:
    """The value added to the slider's aria-valuenow to get seconds.

    The slider runs 0..11 for 4s..15s, so the offset is 4 — but it is derived
    from the two end labels rather than hard-coded, so a UI change that shifts
    the range fails loudly instead of silently rendering the wrong length.
    """
    from opentoken.models.media import VIDEO_SECONDS_MAX, VIDEO_SECONDS_MIN

    if high - low == VIDEO_SECONDS_MAX - VIDEO_SECONDS_MIN:
        return VIDEO_SECONDS_MIN
    return low


def _apply_video_options(
    page, *, ratio: str | None = None, seconds: int | None = None
) -> None:
    """Set aspect ratio and/or duration, then close the panel."""
    if ratio is None and seconds is None:
        return
    if ratio is not None:
        _select_video_ratio(page, ratio)
    if seconds is not None:
        _set_video_duration(page, seconds)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    if seconds is not None:
        _reconcile_duration_with_chip(page, seconds)
    record_event(
        "doubao_video_options_verified",
        requested_ratio=ratio,
        requested_seconds=seconds,
        composer=_composer_snapshot(page),
    )
    _verify_video_chip(page, ratio=ratio, seconds=seconds)


def _composer_button_labels(page) -> list[str]:
    try:
        return [
            " ".join(str(text).split())
            for text in page.locator("#input-engine-container button").all_inner_texts()
        ]
    except Exception:
        return []


def _duration_chip_text(page) -> str:
    for label in _composer_button_labels(page):
        if _DURATION_CHIP_PATTERN.search(label):
            return label
    return ""


def _composer_snapshot(page) -> dict[str, object]:
    """What the composer will send — the state a failed submit hinges on.

    Recorded just before the send click: the selected model, the ratio/duration
    chip Doubao will read, and whether the reference image is still attached.
    """
    return {
        "placeholder": _composer_placeholder(page),
        "model": _current_media_model_label(page),
        "chip": _duration_chip_text(page),
        "attachments": _attachment_count(page),
        "attachment_details": _attachment_details(page),
    }


def _verify_video_chip(page, *, ratio: str | None, seconds: int | None) -> None:
    """Confirm the composer chip reflects what was asked for.

    The chip is the only always-visible readout ("9:16 · 5s · 720P"), so it
    doubles as the check that the panel's controls actually took effect. Failing
    here is deliberate: submitting anyway would render (and bill) a different
    length than the caller asked for.
    """
    chip = _duration_chip_text(page)
    if not chip:
        return
    if ratio is not None and ratio not in chip:
        raise RuntimeError(
            f"Doubao's composer shows '{chip}' after selecting the {ratio} ratio, so "
            "the aspect ratio could not be applied."
        )
    if seconds is not None:
        reached = _chip_seconds(chip)
        if reached is not None and reached != seconds:
            raise RuntimeError(
                f"Doubao's composer shows '{chip}' after setting {seconds}s, so the "
                "duration could not be applied; refusing to render a different length."
            )


# --------------------------------------------------------------------------- #
# Reference images
# --------------------------------------------------------------------------- #
_ATTACHMENT_CARD_SELECTOR = '[data-testid="attachment-image-card"]'

# What the composer cards actually hold. The upload input accepts images only,
# so the interesting part is whether each file became an *image* card with the
# expected name — that is where "the reference never reached Doubao" shows up.
_ATTACHMENT_INFO_JS = """
() => Array.from(document.querySelectorAll('[data-testid="attachment-image-card"]'))
  .map(card => {
    const img = card.querySelector('img');
    return {
      alt: img ? (img.getAttribute('alt') || '') : '',
      kind: card.getAttribute('data-kind') || '',
      text: (card.innerText || '').replace(/\\s+/g, ' ').trim(),
      broken: img ? !img.complete || img.naturalWidth === 0 : null,
    };
  })
"""


def _attachment_count(page) -> int:
    try:
        return int(page.locator(_ATTACHMENT_CARD_SELECTOR).count())
    except Exception:
        return 0


def _attachment_details(page) -> list[dict[str, object]]:
    """Per-card description of the composer attachments (for the task dump)."""
    try:
        result = page.evaluate(_ATTACHMENT_INFO_JS)
    except Exception:
        return []
    return [item for item in result or [] if isinstance(item, dict)]


def _clear_attachments(page) -> None:
    """Drop reference images left over from an earlier turn.

    The video skill keeps attachments in the composer across turns, and a stale
    image would silently become part of the next render's reference set.
    """
    try:
        delete_buttons = page.locator('[data-testid="attachment-delete-btn"]')
        for _ in range(int(delete_buttons.count())):
            delete_buttons.first.click(timeout=4000)
            page.wait_for_timeout(300)
    except Exception as exc:
        logger.debug("doubao_media_clear_attachments_failed error=%s", exc)


def _upload_reference_images(page, paths: list[Path]) -> None:
    """Attach reference images and confirm each one became a card."""
    if not paths:
        return
    _clear_attachments(page)
    upload = page.locator('[data-testid="upload-file-input"]').first
    if not upload.count():
        raise RuntimeError(
            "Doubao's reference image input is missing from the video composer; "
            "the UI may have changed."
        )
    record_event(
        "doubao_reference_upload_started",
        count=len(paths),
        files=[path.name for path in paths],
    )
    upload.set_input_files([str(path) for path in paths], timeout=60000)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _attachment_count(page) >= len(paths):
            logger.info(
                "doubao_media_reference_uploaded count=%d files=%s",
                len(paths),
                [path.name for path in paths],
            )
            # Recorded per card: `alt` is the filename Doubao received, and
            # `broken` catches the case where it accepted the upload but failed
            # to decode it (which is what an audio file used to look like).
            record_event(
                "doubao_reference_uploaded",
                count=len(paths),
                attachments=_attachment_details(page),
            )
            return
        page.wait_for_timeout(400)
    record_event(
        "doubao_reference_upload_failed",
        expected=len(paths),
        actual=_attachment_count(page),
        attachments=_attachment_details(page),
    )
    raise RuntimeError(
        f"Doubao did not accept the reference image(s): {_attachment_count(page)} "
        f"of {len(paths)} appeared in the composer."
    )


# --------------------------------------------------------------------------- #
# DOM probes
# --------------------------------------------------------------------------- #
_MEDIA_STATE_JS = """
({ beforeTurnCount, beforeVideoSrcs }) => {
  const turns = Array.from(document.querySelectorAll('[data-testid="receive_message"]'));
  const fresh = turns.slice(beforeTurnCount);

  const imageUrls = [];
  let imageFinished = false;
  for (const turn of fresh) {
    for (const container of turn.querySelectorAll('[data-testid="message_image_content"]')) {
      const urls = [];
      for (const img of container.querySelectorAll('img')) {
        const src = img.currentSrc || img.src || '';
        if (src && !src.startsWith('data:') && !urls.includes(src)) urls.push(src);
      }
      for (const source of container.querySelectorAll('source[srcset]')) {
        const src = (source.getAttribute('srcset') || '').trim().split(/\\s+/)[0];
        if (src && !src.startsWith('data:') && !urls.includes(src)) urls.push(src);
      }
      for (const src of urls) if (!imageUrls.includes(src)) imageUrls.push(src);
      if (container.getAttribute('data-finished') === 'true' && urls.length) imageFinished = true;
    }
  }

  const videoCovers = [];
  for (const turn of fresh) {
    for (const block of turn.querySelectorAll('[class*="block-video"]')) {
      const cover = block.querySelector('img');
      videoCovers.push(cover ? (cover.currentSrc || cover.src || '') : '');
    }
  }

  const players = [];
  for (const video of document.querySelectorAll('video')) {
    const src = video.currentSrc || video.src || '';
    if (!src || (beforeVideoSrcs || []).includes(src)) continue;
    players.push({
      src,
      width: video.videoWidth || null,
      height: video.videoHeight || null,
      duration: Number.isFinite(video.duration) ? video.duration : null,
    });
  }

  const texts = fresh.map(turn => (turn.innerText || '').replace(/\\s+/g, ' ').trim()).filter(Boolean);
  return {
    turnCount: turns.length,
    imageUrls,
    imageFinished,
    videoCoverCount: videoCovers.length,
    videoPlayers: players,
    texts,
  };
}
"""

_VIDEO_SRCS_JS = """
() => Array.from(document.querySelectorAll('video'))
  .map(video => video.currentSrc || video.src || '')
  .filter(Boolean)
"""

# Doubao gates media generation behind a "确认生成" button in two situations,
# both seen live:
#   A) a compliance notice — "你在本功能中上传、使用的素材，均已获充分授权…"
#   B) an assistant turn offering to continue on paid quota —
#      "今日视频生成免费额度已用完，本次将消耗付费额度。是否继续生成？"
# Neither is a chat message: the only way past them is clicking the button, so
# replying with text (as the parameter read-back needs) does nothing and the
# request just waits until the timeout.
#
# The button carries an arrow icon, so the label is compared whitespace- and
# arrow-stripped. Matches are limited to visible, enabled buttons whose whole
# label is one of these short phrases, which keeps ordinary prose out.
_MEDIA_CONSENT_BUTTON_JS = """
() => {
  const clean = (value) => (value || '')
    .replace(/\\s+/g, '')
    .replace(/[\\u2192\\u203a\\u00bb\\u276f>]/g, '');
  const matches = [];
  for (const node of document.querySelectorAll('button, [role="button"]')) {
    const label = clean(node.innerText);
    if (!label) continue;
    if (!/^(确认生成|继续生成|确认继续)$/.test(label)) continue;
    const visible = !!(node.offsetParent || node.getClientRects().length);
    const disabled = !!node.disabled || node.getAttribute('aria-disabled') === 'true';
    if (!visible || disabled) continue;
    matches.push(node);
  }
  if (!matches.length) return { clicked: false, reason: 'no-button' };
  // Newest first: an older consent card may still be in the transcript.
  const target = matches[matches.length - 1];
  const turn = target.closest('[data-testid="receive_message"]');
  const turnText = turn ? (turn.innerText || '').replace(/\\s+/g, ' ').trim() : '';
  try {
    target.scrollIntoView({ block: 'center' });
  } catch (_error) {}
  try {
    target.click();
  } catch (_error) {
    try {
      target.dispatchEvent(
        new MouseEvent('click', { bubbles: true, cancelable: true, view: window })
      );
    } catch (_inner) {
      return { clicked: false, reason: 'click-failed' };
    }
  }
  return {
    clicked: true,
    label: clean(target.innerText),
    turnText: turnText.slice(0, 400),
    inReceiveMessage: !!turn,
  };
}
"""


def _click_media_consent_button(page) -> dict[str, object]:
    """Click Doubao's "确认生成" gate if it is on screen.

    Returns the JS result: `{"clicked": True, "turnText": ...}` when it clicked,
    `{"clicked": False, "reason": ...}` otherwise. Errors are swallowed into a
    non-click so a UI tweak degrades to the old waiting behaviour instead of
    crashing the render.
    """
    try:
        result = page.evaluate(_MEDIA_CONSENT_BUTTON_JS)
    except Exception as exc:
        logger.debug("doubao_media_consent_probe_failed error=%s", exc)
        return {"clicked": False, "reason": "probe-failed"}
    if not isinstance(result, dict):
        return {"clicked": False, "reason": "unexpected-result"}
    if result.get("clicked"):
        logger.info(
            "doubao_media_consent_clicked label=%s in_receive_message=%s turn=%s",
            result.get("label"),
            result.get("inReceiveMessage"),
            str(result.get("turnText") or "")[:200],
        )
    return result

_CLICK_VIDEO_PLAYER_JS = """
() => {
  const wrappers = Array.from(document.querySelectorAll('[class*="video-player-wrapper"]'));
  const target = wrappers[wrappers.length - 1];
  if (!target) return false;
  target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
  return true;
}
"""


def _media_state(page, *, before_turn_count: int, before_video_srcs: list[str]) -> dict[str, object]:
    return page.evaluate(
        _MEDIA_STATE_JS,
        {"beforeTurnCount": before_turn_count, "beforeVideoSrcs": before_video_srcs},
    )


def _state_for_dump(state: dict[str, object]) -> dict[str, object]:
    """The part of a polled state worth putting in a task file."""
    return {
        "turn_count": state.get("turnCount"),
        "texts": state.get("texts") or [],
        "video_cover_count": state.get("videoCoverCount"),
        "video_players": state.get("videoPlayers") or [],
        "image_urls": state.get("imageUrls") or [],
        "image_finished": state.get("imageFinished"),
    }


def _state_signature(state: dict[str, object]) -> str:
    """Stable digest of a polled state, so only real changes get recorded.

    Polling every 3s for ten minutes would otherwise write 200 identical
    timeline entries and mask the one transition that matters.
    """
    return json.dumps(
        [
            state.get("turnCount"),
            state.get("texts"),
            state.get("videoCoverCount"),
            state.get("videoPlayers"),
            state.get("imageUrls"),
        ],
        ensure_ascii=False,
        default=str,
    )


def _turn_count(page) -> int:
    return int(
        page.evaluate(
            "() => document.querySelectorAll('[data-testid=\"receive_message\"]').length"
        )
        or 0
    )


def _compact_whitespace(value: str) -> str:
    """Drop all whitespace so markers survive UI formatting.

    `_MEDIA_STATE_JS` collapses runs of whitespace to a single space, so a
    bolded/wrapped phrase like "免费次数\\n用完了" arrives as
    "免费次数 用完了" and would not match "免费次数用完". Every marker used here
    is Chinese, where dropping spaces cannot create a false match.
    """
    return re.sub(r"\s+", "", value or "")


def _find_marker(text: str, markers: tuple[str, ...]) -> str | None:
    if not text:
        return None
    compact = _compact_whitespace(text)
    for marker in markers:
        if marker in text or _compact_whitespace(marker) in compact:
            return marker
    return None


def _media_paid_quota_allowed() -> bool:
    """Whether the gateway may accept Doubao's offer to spend *paid* quota.

    Clicking "确认生成" on "本次将消耗付费额度" costs real money, so it is the one
    consent that must be a policy decision. Default is allow (an unattended
    gateway that silently stalls on a paywall is not useful), but it can be
    turned into a hard 429 with ``OPENTOKEN_DOUBAO_ALLOW_PAID_MEDIA=0``.
    """
    raw = os.environ.get("OPENTOKEN_DOUBAO_ALLOW_PAID_MEDIA", "1")
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def _raise_for_media_failure(
    state: dict[str, object], *, muted: frozenset[str] = frozenset()
) -> None:
    """Abort as soon as the transcript says no render is coming.

    Both checks run before the caller looks for media, so a refusal or an
    exhausted quota ends the request in seconds instead of waiting out
    `timeout`. Quota is a 429 (retry tomorrow / other account), a refusal is a
    502 — hence the separate marker sets and exception types.

    ``muted`` holds turn texts that were already answered by a click or a
    confirmation message. Without it, the "是否继续生成？" turn stays in the
    transcript after its button is clicked and would abort the render that the
    click just started.
    """
    texts = [
        str(text)
        for text in state.get("texts") or []
        if _compact_whitespace(str(text)) not in muted
    ]
    for text in texts:
        paid = _find_marker(text, _DOUBAO_MEDIA_PAID_QUOTA_MARKERS)
        if paid:
            # Paid quota is mentioned, but that alone is not a stop:
            #   * a notice ("…并使用优先生成通道") means the render is already
            #     queued on the paid channel — the live case that used to abort;
            #   * an offer only blocks when the policy forbids spending, and
            #     `_ConfirmationTracker.handle` already clicked it otherwise.
            if _find_marker(text, _DOUBAO_MEDIA_PAID_OFFER_MARKERS) and not (
                _media_paid_quota_allowed()
            ):
                raise ProviderRateLimitError(
                    "Doubao's free media quota is exhausted and it is asking whether "
                    "to continue on paid quota. Spending it automatically is disabled "
                    "by OPENTOKEN_DOUBAO_ALLOW_PAID_MEDIA=0; approve it in the Doubao "
                    f"UI, then retry. Upstream said: {text}"
                )
            logger.warning("doubao_media_paid_quota_in_use text=%s", text[:200])
            continue
        if _find_marker(text, _DOUBAO_MEDIA_HARD_QUOTA_MARKERS):
            raise ProviderRateLimitError(f"Doubao media quota exhausted: {text}")
        if _find_marker(text, _DOUBAO_MEDIA_FAILURE_MARKERS):
            raise RuntimeError(f"Doubao media generation failed: {text}")
    # A refusal can also arrive as an assistant turn with no media at all; the
    # caller's timeout reports it, so nothing else to do here.



@dataclass
class _ConfirmationTracker:
    """Get past Doubao's two kinds of "waiting for you" turn.

    Doubao holds a media request in two shapes, and they need different answers:

    * **A gate button** — ``确认生成``. Two flavours: a compliance notice ("你在
      本功能中上传、使用的素材，均已获充分授权…") and a paid-quota offer ("今日
      视频生成免费额度已用完，本次将消耗付费额度。是否继续生成？"). Only a click
      moves these forward; a chat message does nothing.
    * **A parameter read-back** — "我将为你生成…请先确认以下参数…确认后我再开始
      生成视频". There is no button; the render starts once a follow-up user turn
      says to go ahead.

    Both leave the transcript without a media card, so a poll-only loop used to
    sit there until the timeout and then blame the render.

    Safeguards, because a false positive submits a second render (and, for the
    paid gate, spends money):

    * the read-back text must be **stable across two polls** — a turn still
      streaming has not finished deciding to wait;
    * each transcript turn index is answered at most once;
    * the button path is capped by :data:`_MAX_CONSENT_CLICKS`, and the text
      path by :data:`_MAX_CONFIRMATION_ROUNDS`.
    """

    kind: str
    rounds: int = 0
    clicks: int = 0
    answered: set[int] = field(default_factory=set)
    # Normalized turn texts whose consent was already granted; the failure scan
    # must ignore them or it would abort the render the consent just started.
    muted: set[str] = field(default_factory=set)
    _seen: tuple[int, str] | None = None

    def _pending(
        self, state: dict[str, object], *, before_turn_count: int
    ) -> tuple[int, str] | None:
        turn_count = int(state.get("turnCount") or 0)
        if turn_count <= before_turn_count:
            return None
        texts = [str(text) for text in state.get("texts") or []]
        if not texts:
            return None
        index = turn_count - 1
        if index in self.answered:
            return None
        latest = texts[-1]
        if not _find_marker(latest, _DOUBAO_CONFIRMATION_MARKERS):
            return None
        return index, latest

    def handle(
        self,
        page,
        state: dict[str, object],
        *,
        before_turn_count: int,
    ) -> str:
        """Advance Doubao past a gate or a read-back.

        Returns ``"clicked"``, ``"text"`` or ``"none"`` so the caller can tell
        whether anything was submitted (and re-click send if the gate was hit
        before the prompt ever went out).
        """
        latest = self._latest_text(state, before_turn_count=before_turn_count)
        # Refuse the paid-quota offer before clicking: the button would spend
        # money, so the policy check has to come first. A *notice* that paid
        # quota is being used has no offer in it and needs no decision.
        if latest and _find_marker(latest, _DOUBAO_MEDIA_PAID_QUOTA_MARKERS):
            offering = bool(_find_marker(latest, _DOUBAO_MEDIA_PAID_OFFER_MARKERS))
            if offering and not _media_paid_quota_allowed():
                raise ProviderRateLimitError(
                    "Doubao's free media quota is exhausted and it is asking whether "
                    "to continue on paid quota. Spending it automatically is disabled "
                    "by OPENTOKEN_DOUBAO_ALLOW_PAID_MEDIA=0; approve it in the Doubao "
                    f"UI, then retry. Upstream said: {latest}"
                )
            logger.warning(
                "doubao_media_paid_quota_consent kind=%s offering=%s text=%s",
                self.kind,
                offering,
                latest[:200],
            )

        if _compact_whitespace(latest) in self.muted:
            # This turn's consent was already granted and Doubao has not moved on
            # yet, so there is nothing new to answer.
            return "none"

        if self.clicks < _MAX_CONSENT_CLICKS:
            result = _click_media_consent_button(page)
            if result.get("clicked"):
                self.clicks += 1
                turn_text = str(result.get("turnText") or latest or "")
                self.muted.add(_compact_whitespace(turn_text))
                if latest:
                    self.muted.add(_compact_whitespace(latest))
                logger.info(
                    "doubao_media_consent_granted kind=%s clicks=%d", self.kind, self.clicks
                )
                record_event(
                    "doubao_consent_granted",
                    media_kind=self.kind,
                    clicks=self.clicks,
                    label=result.get("label"),
                    turn_text=turn_text,
                )
                return "clicked"

        answered = self.answer(page, state, before_turn_count=before_turn_count)
        return "text" if answered else "none"

    @staticmethod
    def _latest_text(state: dict[str, object], *, before_turn_count: int) -> str:
        turn_count = int(state.get("turnCount") or 0)
        if turn_count <= before_turn_count:
            return ""
        texts = [str(text) for text in state.get("texts") or []]
        return texts[-1] if texts else ""

    def answer(self, page, state: dict[str, object], *, before_turn_count: int) -> bool:
        """Send the confirmation if Doubao is waiting for one. True when sent."""
        pending = self._pending(state, before_turn_count=before_turn_count)
        if pending is None:
            self._seen = None
            return False
        index, text = pending
        if self._seen != (index, text):
            # First sighting (or still streamed further): wait for a poll that
            # sees the same final text before replying.
            self._seen = (index, text)
            return False
        if self.rounds >= _MAX_CONFIRMATION_ROUNDS:
            raise RuntimeError(
                "Doubao keeps asking for confirmation without starting the "
                f"render (answered {self.rounds} times). Last request: {text[:300]}"
            )
        self.rounds += 1
        self.answered.add(index)
        self._seen = None
        logger.info(
            "doubao_media_confirmation_required kind=%s round=%d text=%s",
            self.kind,
            self.rounds,
            text[:200],
        )
        record_event(
            "doubao_confirmation_required",
            media_kind=self.kind,
            round=self.rounds,
            text=text,
            reply=self._reply_text(),
        )
        _doubao_fill_composer(page, self._reply_text())
        _doubao_click_send_button(page)
        logger.info(
            "doubao_media_confirmation_sent kind=%s round=%d", self.kind, self.rounds
        )
        return True

    def _reply_text(self) -> str:
        return f"确认，请按上述参数开始生成{self.kind}。"


# --------------------------------------------------------------------------- #
# SSE parsing (images)
# --------------------------------------------------------------------------- #
def _iter_sse_data(body: str):
    for block in body.split("\n\n"):
        data_lines = [
            line[5:].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        payload = "\n".join(line for line in data_lines if line)
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def _iter_creation_blocks(node):
    if isinstance(node, dict):
        block = node.get("creation_block")
        if isinstance(block, dict):
            yield block
        for value in node.values():
            yield from _iter_creation_blocks(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_creation_blocks(value)


def _first_image_url(creation: dict[str, object]) -> tuple[str, int | None, int | None]:
    image = creation.get("image")
    if not isinstance(image, dict):
        return "", None, None
    for key in ("image_ori_raw", "image_ori", "image_preview", "image_thumb"):
        variant = image.get(key)
        if not isinstance(variant, dict):
            continue
        url = str(variant.get("url") or "").strip()
        if not url:
            continue
        width = variant.get("width")
        height = variant.get("height")
        return (
            url,
            int(width) if isinstance(width, int) else None,
            int(height) if isinstance(height, int) else None,
        )
    return "", None, None


def extract_doubao_creation_images(body: str) -> list[MediaAsset]:
    """Pull finished renders out of a Doubao ``/chat/completion`` SSE body.

    Only ``image.status: 2`` creations are returned — ``status: 1`` is the
    placeholder chunk Doubao emits while it is still rendering (it has the
    dimensions but no URL yet).
    """
    assets: list[MediaAsset] = []
    seen: set[str] = set()
    for event in _iter_sse_data(body):
        for block in _iter_creation_blocks(event):
            creations = block.get("creations")
            if not isinstance(creations, list):
                continue
            for creation in creations:
                if not isinstance(creation, dict):
                    continue
                image = creation.get("image")
                if not isinstance(image, dict):
                    continue
                if image.get("status") not in (2, "2"):
                    continue
                url, width, height = _first_image_url(creation)
                if not url or url in seen:
                    continue
                seen.add(url)
                revised_prompt = None
                gen_params = image.get("gen_params")
                if isinstance(gen_params, dict):
                    raw_prompt = gen_params.get("prompt")
                    if isinstance(raw_prompt, str) and raw_prompt.strip():
                        revised_prompt = raw_prompt.strip()
                assets.append(
                    MediaAsset(
                        url=url,
                        media_type="image/png" if ".png" in url or "image_raw" in url else "image/jpeg",
                        width=width,
                        height=height,
                        revised_prompt=revised_prompt,
                    )
                )
    return assets


# --------------------------------------------------------------------------- #
# Upstream submit capture (task dumps)
# --------------------------------------------------------------------------- #
# The composer's own signed request. Capturing its POST body is the only direct
# evidence of whether the reference image was part of the submission: the image
# is uploaded separately and referenced by resource id, so a missing id here
# means Doubao rendered from text alone.
_COMPLETION_URL_MARKERS = ("chat/completion",)
_MAX_CAPTURED_POST_DATA_CHARS = 20000


def _maybe_json(text: object) -> object:
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    if len(text) > _MAX_CAPTURED_POST_DATA_CHARS:
        return text[:_MAX_CAPTURED_POST_DATA_CHARS] + f"… ({len(text)} chars total)"
    return text


def _start_completion_capture(page, recorder=None):
    """Record every ``/chat/completion`` POST Doubao's own composer fires.

    The body is read from the request (``post_data``), not the response: the
    response is a long-lived SSE stream, and awaiting its text inside an event
    callback would block the render poll loop.

    `recorder` is passed explicitly: Playwright invokes these callbacks on its
    own driver thread, where the request thread's `ContextVar` is not visible, so
    `record_section` would silently no-op.
    """
    records: list[dict[str, object]] = []

    def handle_response(response) -> None:
        try:
            url = str(getattr(response, "url", ""))
            if not any(marker in url for marker in _COMPLETION_URL_MARKERS):
                return
            request = getattr(response, "request", None)
            if request is None:
                return
            if str(getattr(request, "method", "")).upper() != "POST":
                return
            try:
                post_data = request.post_data
            except Exception:
                post_data = None
            records.append(
                {
                    "url": url,
                    "status": getattr(response, "status", None),
                    "content_type": str(
                        (getattr(response, "headers", {}) or {}).get("content-type", "")
                    ),
                    "post_data": _maybe_json(post_data),
                }
            )
            if recorder is not None:
                recorder.update(upstream={"completions": list(records)})
        except Exception as exc:  # noqa: BLE001 - capture must never break a render
            logger.debug("doubao_media_completion_capture_failed error=%s", exc)

    page.on("response", handle_response)
    return records, handle_response


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def generate_doubao_images(
    page,
    *,
    prompt: str,
    model: MediaModel,
    count: int,
    timeout_seconds: float = 240.0,
    per_image_timeout_seconds: float = 150.0,
) -> list[MediaAsset]:
    """Render `count` images one at a time (the web UI has no "n" control)."""
    assets: list[MediaAsset] = []
    _enter_skill(
        page,
        label=DOUBAO_IMAGE_SKILL_LABEL,
        placeholder=_DOUBAO_IMAGE_COMPOSER_PLACEHOLDER,
    )
    _select_media_model(page, media_ui_label(model))
    deadline = time.monotonic() + timeout_seconds
    for _ in range(max(1, count)):
        remaining = deadline - time.monotonic()
        if remaining <= 5:
            break
        assets.extend(
            _generate_one_image(
                page,
                prompt=prompt,
                timeout_seconds=min(per_image_timeout_seconds, remaining),
            )
        )
    if not assets:
        raise RuntimeError("Doubao image generation produced no images.")
    return assets


def _generate_one_image(page, *, prompt: str, timeout_seconds: float) -> list[MediaAsset]:
    before_turn_count = _turn_count(page)
    captured: list[object] = []

    def handle_response(response) -> None:
        # Keep a handful instead of only the first: if Doubao pauses for a
        # parameter confirmation, the first completion is that question, and the
        # render's SSE arrives on a later turn.
        if len(captured) >= _MAX_CAPTURED_COMPLETIONS:
            return
        try:
            if "chat/completion" not in str(getattr(response, "url", "")):
                return
            request = getattr(response, "request", None)
            if request is None or str(getattr(request, "method", "")).upper() != "POST":
                return
            content_type = str((getattr(response, "headers", {}) or {}).get("content-type", "")).lower()
            if content_type and "text/event-stream" not in content_type:
                return
        except Exception:
            return
        captured.append(response)

    page.on("response", handle_response)
    try:
        _doubao_fill_composer(page, prompt)
        _doubao_click_send_button(page)
        dom_urls = _wait_for_doubao_images(
            page,
            before_turn_count=before_turn_count,
            timeout_seconds=timeout_seconds,
        )
    finally:
        _detach_page_listener(page, "response", handle_response)

    for response in captured:
        try:
            assets = extract_doubao_creation_images(response.text())
        except Exception:
            continue
        if assets:
            return assets
    return [
        MediaAsset(url=url, media_type="image/png") for url in dom_urls
    ]


def _wait_for_doubao_images(
    page,
    *,
    before_turn_count: int,
    timeout_seconds: float,
) -> list[str]:
    deadline = time.monotonic() + timeout_seconds
    confirmations = _ConfirmationTracker(kind="图片")
    last_state: dict[str, object] = {}
    while time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        last_state = _media_state(page, before_turn_count=before_turn_count, before_video_srcs=[])
        # Consent gates (compliance notice / paid quota) need a click, not a
        # message, so handle them before deciding the request is hopeless.
        if confirmations.handle(page, last_state, before_turn_count=before_turn_count) == "clicked":
            continue
        _raise_for_media_failure(last_state, muted=frozenset(confirmations.muted))
        urls = [str(url) for url in last_state.get("imageUrls") or []]
        if urls and (last_state.get("imageFinished") or last_state.get("turnCount", 0) > before_turn_count):
            # Give the card a beat to finish swapping its blur-up placeholder
            # for the real render before trusting the URL list.
            page.wait_for_timeout(500)
            final_state = _media_state(
                page, before_turn_count=before_turn_count, before_video_srcs=[]
            )
            final_urls = [str(url) for url in final_state.get("imageUrls") or []] or urls
            if final_state.get("imageFinished") or final_urls:
                return final_urls
        # The read-back ("请先确认参数") path is already handled by `handle()` above,
        # which also covers the click-only gates.
    urls = [str(url) for url in last_state.get("imageUrls") or []]
    if urls:
        return urls
    texts = " ".join(str(text) for text in last_state.get("texts") or [])
    raise RuntimeError(
        "Doubao image generation timed out after "
        f"{int(timeout_seconds)}s without an image card"
        + (f" (last reply: {texts[:300]})" if texts else ".")
    )


def generate_doubao_video(
    page,
    *,
    prompt: str,
    model: MediaModel,
    timeout_seconds: float = 600.0,
    player_timeout_seconds: float = 90.0,
    seconds: int | None = None,
    size: str | None = None,
    reference_images: list[Path] | tuple[Path, ...] = (),
) -> MediaAsset:
    """Submit a video render and wait for Doubao's asynchronous delivery.

    `reference_images` are local files (the route materializes uploads/URLs/data
    URIs first) and `size` is a ``WxH`` string, which is projected onto the
    closest aspect ratio Doubao offers. Doubao's own UI takes images only — the
    route rejects audio before it gets this far.
    """
    _enter_skill(
        page,
        label=DOUBAO_VIDEO_SKILL_LABEL,
        placeholder=_DOUBAO_VIDEO_COMPOSER_PLACEHOLDER,
    )
    record_event(
        "doubao_skill_entered",
        skill=DOUBAO_VIDEO_SKILL_LABEL,
        placeholder=_composer_placeholder(page),
    )
    _select_media_model(page, media_ui_label(model))
    record_event("doubao_model_selected", label=media_ui_label(model))

    references = [Path(path) for path in reference_images]
    if references and not model.supports_reference_image:
        raise RuntimeError(
            f"Doubao's {model.label} cannot take a reference image; use "
            "doubao/seedance-2.0 or doubao/seedance-2.0-mini instead."
        )
    ratio = None
    if size:
        from opentoken.models.media import video_ratio_for_size

        ratio = video_ratio_for_size(size)
    # Attach and configure before sending: the composer starts a fresh turn each
    # time the prompt is submitted, and options/attachments belong to that turn.
    _upload_reference_images(page, references)
    _apply_video_options(page, ratio=ratio, seconds=seconds)
    record_event(
        "doubao_video_options_applied",
        requested_ratio=ratio,
        requested_seconds=seconds,
        composer=_composer_snapshot(page),
    )
    logger.info(
        "doubao_media_video_request model=%s ratio=%s seconds=%s references=%d",
        model.id,
        ratio,
        seconds,
        len(references),
    )

    before_turn_count = _turn_count(page)
    before_video_srcs: list[str] = list(page.evaluate(_VIDEO_SRCS_JS) or [])

    # What will actually go out, captured immediately before the click: a
    # reference that silently vanished from the composer shows up here.
    record_event(
        "doubao_prompt_submit",
        prompt=prompt,
        composer=_composer_snapshot(page),
    )
    # Attached before the click: the completion POST is fired by that click, and
    # a listener added afterwards can miss the response event entirely.
    _captured, capture_handler = _start_completion_capture(page, active_recorder())
    _doubao_fill_composer(page, prompt)
    _doubao_click_send_button(page)
    record_event("doubao_prompt_submitted", before_turn_count=before_turn_count)

    deadline = time.monotonic() + timeout_seconds
    player_clicked = False
    confirmations = _ConfirmationTracker(kind="视频")
    last_state: dict[str, object] = {}
    last_signature = ""
    try:
        while time.monotonic() < deadline:
            page.wait_for_timeout(3000)
            last_state = _media_state(
                page,
                before_turn_count=before_turn_count,
                before_video_srcs=before_video_srcs,
            )
            signature = _state_signature(last_state)
            if signature != last_signature:
                last_signature = signature
                record_event("doubao_transcript", **_state_for_dump(last_state))
            # Consent gates come first: a compliance notice or the paid-quota offer
            # blocks the render until its "确认生成" button is clicked, and clicking
            # is the only thing that works (a chat message just sits in the history).
            consent = confirmations.handle(
                page, last_state, before_turn_count=before_turn_count
            )
            if consent == "clicked":
                # If the gate appeared before the prompt was ever submitted, the
                # click only dismissed it — send the prompt again. `turnCount` is
                # the signal: no new assistant turn means Doubao never received it.
                if int(last_state.get("turnCount") or 0) <= before_turn_count:
                    logger.info("doubao_media_consent_before_send kind=视频 resending")
                    _doubao_fill_composer(page, prompt)
                    _doubao_click_send_button(page)
                continue
            _raise_for_media_failure(last_state, muted=frozenset(confirmations.muted))
            players = last_state.get("videoPlayers") or []
            if players:
                player = players[-1]
                if isinstance(player, dict) and player.get("src"):
                    asset = MediaAsset(
                        url=str(player["src"]),
                        media_type="video/mp4",
                        width=_as_int(player.get("width")),
                        height=_as_int(player.get("height")),
                        duration_seconds=_as_float(player.get("duration")),
                    )
                    record_event("doubao_video_ready", **_asset_fields(asset))
                    return asset
            # The <video> element only mounts once the card's player is opened; the
            # cover image is our signal that the render finished.
            if last_state.get("videoCoverCount") and not player_clicked:
                player_clicked = True
                _open_video_player(page)
                continue
            # Doubao may have answered with "请先确认参数…确认后我再开始生成视频" and be
            # waiting for a follow-up turn. `handle()` above answers it; without that
            # the loop only ever sees text and reports a timeout even though nothing
            # was ever submitted to the renderer.
    finally:
        _detach_page_listener(page, "response", capture_handler)

    players = last_state.get("videoPlayers") or []
    if players:
        player = players[-1]
        if isinstance(player, dict) and player.get("src"):
            asset = MediaAsset(url=str(player["src"]), media_type="video/mp4")
            record_event("doubao_video_ready", **_asset_fields(asset))
            return asset

    # The cover card can appear a little before the player is ready; give it one
    # last chance within the player budget.
    if last_state.get("videoCoverCount"):
        _open_video_player(page)
        src = _wait_for_video_src(page, before_video_srcs, timeout_seconds=player_timeout_seconds)
        if src:
            record_event("doubao_video_ready", url=src)
            return MediaAsset(url=src, media_type="video/mp4")

    texts = " ".join(str(text) for text in last_state.get("texts") or [])
    record_event("doubao_video_timeout", **_state_for_dump(last_state))
    raise RuntimeError(
        "Doubao video generation timed out after "
        f"{int(timeout_seconds)}s without a playable video"
        + (f" (last reply: {texts[:300]})" if texts else ".")
    )


def _open_video_player(page) -> None:
    opened = False
    try:
        opened = bool(page.evaluate(_CLICK_VIDEO_PLAYER_JS))
    except Exception:
        opened = False
    if opened:
        page.wait_for_timeout(1500)
        return
    try:
        page.locator('[class*="video-player-wrapper"]').last.click(timeout=5000, force=True)
    except Exception:
        pass
    page.wait_for_timeout(1500)


def _wait_for_video_src(page, before_video_srcs: list[str], *, timeout_seconds: float) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(1500)
        try:
            srcs = [str(src) for src in page.evaluate(_VIDEO_SRCS_JS) or []]
        except Exception:
            srcs = []
        fresh = [src for src in srcs if src not in before_video_srcs]
        if fresh:
            return fresh[-1]
    return ""


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _asset_fields(asset: MediaAsset) -> dict[str, object]:
    return {
        "url": asset.url,
        "media_type": asset.media_type,
        "width": asset.width,
        "height": asset.height,
        "duration_seconds": asset.duration_seconds,
    }


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = [
    "DOUBAO_IMAGE_SKILL_LABEL",
    "DOUBAO_VIDEO_SKILL_LABEL",
    "MediaAsset",
    "extract_doubao_creation_images",
    "generate_doubao_images",
    "generate_doubao_video",
    "media_ui_label",
]
