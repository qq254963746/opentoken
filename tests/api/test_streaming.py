from opentoken.api.streaming import ProtocolMarkupProjector, chunk_visible_text, strip_tool_protocol_markup


def test_protocol_markup_projector_preserves_fragmented_think_tags_and_hides_tool_markup() -> None:
    projector = ProtocolMarkupProjector()

    pieces = [
        "<thi",
        "nk>先想",
        "一想</think><tool_",
        'call id="call_weather_1" name="get_weather">',
        '{"location":"Tokyo"}',
        "</tool_call><final_",
        "answer>最终答案</final_answer>",
    ]

    deltas = [projector.push(piece) for piece in pieces]

    assert deltas == [
        "",
        "<think>先想",
        "一想</think>",
        "",
        "",
        "",
        "最终答案",
    ]
    assert projector.visible_text == "<think>先想一想</think>最终答案"


def test_strip_tool_protocol_markup_keeps_think_but_removes_tool_and_final_tags() -> None:
    assert (
        strip_tool_protocol_markup(
            '<think>先想一想</think><tool_calls>[{"name":"get_weather","arguments":{"location":"Tokyo"}}]</tool_calls><final_answer>最终答案</final_answer>'
        )
        == "<think>先想一想</think>最终答案"
    )


def test_strip_tool_protocol_markup_can_hide_think_for_chat_completions() -> None:
    assert (
        strip_tool_protocol_markup(
            '<think>先想一想</think><tool_calls>[{"name":"get_weather","arguments":{"location":"Tokyo"}}]</tool_calls><final_answer>最终答案</final_answer>',
            include_think=False,
        )
        == "最终答案"
    )


def test_protocol_markup_projector_can_hide_fragmented_think_for_chat_completions() -> None:
    projector = ProtocolMarkupProjector(include_think=False)

    pieces = [
        "<thi",
        "nk>先想",
        "一想</think><final_",
        "answer>最终答案</final_answer>",
    ]

    deltas = [projector.push(piece) for piece in pieces]

    assert deltas == ["", "", "", "最终答案"]
    assert projector.visible_text == "最终答案"


def test_chunk_visible_text_preserves_complete_think_tag_boundaries() -> None:
    assert chunk_visible_text("<think>先想一想</think>最终答案") == [
        "<think>",
        "先想一想",
        "</think>",
        "最终答案",
    ]


def test_chunk_visible_text_handles_nested_think_blocks() -> None:
    """嵌套 think:"<think>a<think>b</think>c</think>d" 之前非贪婪 regex 只匹配
    最内层一对,留下 "c</think>d" 走 plain → 客户端看到孤立 </think>。栈式扫描
    取最外层后,内层标签留在外层 body 里原样输出,外层结束 d 仍是 plain。"""
    result = chunk_visible_text("<think>a<think>b</think>c</think>d")
    # 把 chunks 拼回去应该等于原文（仅 chunking 拆分,无字符丢失）
    assert "".join(result) == "<think>a<think>b</think>c</think>d"
    # 第一个 chunk 是外层 <think>,最后包含 d 的 chunk 不该是孤立的 </think>
    assert result[0] == "<think>"
    # </think> 应该恰好出现一次作为独立 chunk（外层闭合）
    standalone_close = [c for c in result if c == "</think>"]
    assert len(standalone_close) == 1


def test_projector_handles_tags_split_character_by_character() -> None:
    """Incremental projector: feeding one character per push must reassemble
    tags across the unparsed-tail boundary and yield the same final visible
    text as a single batch projection (no chars lost or tags mis-split)."""
    raw = '<think>reason</think><tool_call id="x" name="t">{}</tool_call>answer'
    projector = ProtocolMarkupProjector()
    out = "".join(projector.push(ch) for ch in raw)
    assert out == "<think>reason</think>answer"
    assert projector.visible_text == "<think>reason</think>answer"
    # Equivalent to the one-shot projection.
    assert strip_tool_protocol_markup(raw) == "<think>reason</think>answer"


def test_project_emits_close_think_even_when_inner_hidden_tag_was_open() -> None:
    """Malformed nesting: a <tool_call> opens inside <think> and never closes
    before </think> fires. The visible region is "<think>a" + "</think>c"; the
    close MUST be emitted to keep the markup balanced, even though hidden
    state was True at the moment </think> arrived. The old code suppressed
    the close, leaving downstream `<think>` block parsers permanently open."""
    text = '<think>a<tool_call id="x" name="t">b</think>c'
    assert strip_tool_protocol_markup(text) == "<think>a</think>c"


def test_project_via_projector_handles_unclosed_inner_hidden_tag() -> None:
    """Same scenario but driven through the streaming projector, since SSE
    clients see the projector's output, not strip_tool_protocol_markup's."""
    projector = ProtocolMarkupProjector()
    final = projector.push('<think>reason</think>')  # baseline
    assert projector.visible_text == "<think>reason</think>"

    projector2 = ProtocolMarkupProjector()
    projector2.push('<think>reason here')
    projector2.push('<tool_call id="x" name="t">{}</tool_call>')
    # The close of think must eventually appear once we send it.
    projector2.push('</think>after')
    assert projector2.visible_text == "<think>reason here</think>after"


def test_run_blocking_with_heartbeat_emits_keepalives_while_a_call_blocks() -> None:
    """A non-stream provider call must not leave the SSE connection silent.

    Browser adapters answer tool-carrying requests through `router.chat`, which
    can block for a minute; without keepalives clients hit their read timeout and
    abort a request that is still progressing.
    """
    import threading
    import time

    from opentoken.api.routes.chat import _run_blocking_with_heartbeat

    release = threading.Event()

    def slow_call() -> str:
        release.wait(timeout=5)
        return "answer"

    kinds: list[str] = []

    def consume() -> None:
        for kind, value in _run_blocking_with_heartbeat(slow_call, interval=0.01):
            kinds.append(kind)
            if kind == "item":
                assert value == "answer"

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    time.sleep(0.1)
    release.set()
    worker.join(timeout=5)

    assert kinds.count("heartbeat") >= 2
    assert kinds[-1] == "item"


def test_run_blocking_with_heartbeat_surfaces_the_call_error() -> None:
    from opentoken.api.routes.chat import _run_blocking_with_heartbeat

    def boom() -> str:
        raise RuntimeError("upstream died")

    events = list(_run_blocking_with_heartbeat(boom, interval=0.01))

    assert events[0][0] == "error"
    assert isinstance(events[0][1], RuntimeError)
    assert len(events) == 1
