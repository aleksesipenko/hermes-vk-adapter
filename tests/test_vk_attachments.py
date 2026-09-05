"""Reply, forward and attachment context must be bounded but never dropped."""

from __future__ import annotations

import pytest

from plugins.vk.attachments import (
    ContextLimits,
    summarize_attachment,
    summarize_message_context,
)

TIGHT = ContextLimits(max_depth=2, max_messages=3, max_text_chars=60, max_attachments=3)


# ── per-attachment summaries ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("attachment", "expected"),
    [
        ({"type": "photo", "photo": {}}, "[photo]"),
        ({"type": "doc", "doc": {"title": "report.pdf"}}, "[document: report.pdf]"),
        ({"type": "audio_message", "audio_message": {}}, "[voice message]"),
        (
            {"type": "link", "link": {"url": "https://example.com", "title": "Example"}},
            "[link: Example https://example.com]",
        ),
        ({"type": "sticker", "sticker": {"sticker_id": 12}}, "[sticker 12]"),
        ({"type": "poll", "poll": {"question": "Which one?"}}, "[poll: Which one?]"),
        ({"type": "video", "video": {"title": "Demo"}}, "[video: Demo]"),
        ({"type": "audio", "audio": {"artist": "A", "title": "B"}}, "[audio: A - B]"),
        ({"type": "wall", "wall": {"owner_id": -1, "id": 2}}, "[wall post wall-1_2]"),
        ({"type": "story", "story": {}}, "[story]"),
        ({"type": "graffiti", "graffiti": {}}, "[graffiti attachment]"),
    ],
)
def test_every_attachment_type_gets_an_explicit_summary(attachment, expected):
    """Nothing is silently dropped: an unknown type still names itself."""
    assert summarize_attachment(attachment) == expected


def test_a_malformed_attachment_is_described_rather_than_dropped():
    assert summarize_attachment({"type": "doc"}) == "[document]"
    assert summarize_attachment({}) == "[unknown attachment]"
    assert summarize_attachment("nonsense") is None


def test_attachment_summaries_are_bounded():
    long_title = "x" * 5000
    summary = summarize_attachment({"type": "doc", "doc": {"title": long_title}})

    assert len(summary) < 250


def test_link_summary_survives_a_missing_title():
    assert (
        summarize_attachment({"type": "link", "link": {"url": "https://example.com"}})
        == "[link: https://example.com]"
    )


# ── reply context ─────────────────────────────────────────────────────────


def test_a_reply_contributes_its_text_and_attachments():
    context = summarize_message_context(
        {
            "reply_message": {
                "from_id": 5,
                "text": "the original question",
                "attachments": [{"type": "doc", "doc": {"title": "spec.pdf"}}],
            }
        },
        TIGHT,
    )

    assert "the original question" in context
    assert "[document: spec.pdf]" in context
    assert "reply" in context.lower()


def test_a_message_without_context_produces_nothing():
    assert summarize_message_context({"text": "hi"}, TIGHT) == ""
    assert summarize_message_context({}, TIGHT) == ""


# ── forwards ──────────────────────────────────────────────────────────────


def test_forwarded_messages_are_included_in_order():
    context = summarize_message_context(
        {"fwd_messages": [{"from_id": 1, "text": "first"}, {"from_id": 2, "text": "second"}]},
        TIGHT,
    )

    assert context.index("first") < context.index("second")


def test_nested_forwards_stop_at_the_depth_limit():
    deep = {"from_id": 9, "text": "level4"}
    for level in (3, 2, 1):
        deep = {"from_id": 9, "text": f"level{level}", "fwd_messages": [deep]}

    context = summarize_message_context({"fwd_messages": [deep]}, TIGHT)

    assert "level1" in context
    assert "level4" not in context


def test_the_number_of_forwarded_messages_is_capped():
    forwards = [{"from_id": index, "text": f"msg{index}"} for index in range(50)]

    context = summarize_message_context({"fwd_messages": forwards}, TIGHT)

    assert "msg0" in context
    assert "msg40" not in context
    assert "truncated" in context.lower()


def test_total_context_text_is_capped():
    forwards = [{"from_id": 1, "text": "y" * 500} for _ in range(10)]

    context = summarize_message_context({"fwd_messages": forwards}, TIGHT)

    assert len(context) <= TIGHT.max_text_chars + 200  # cap plus the truncation notice
    assert "truncated" in context.lower()


def test_the_attachment_count_is_capped_and_the_excess_is_reported():
    attachments = [{"type": "photo", "photo": {}} for _ in range(20)]

    context = summarize_message_context(
        {"reply_message": {"from_id": 1, "text": "look", "attachments": attachments}},
        TIGHT,
    )

    assert context.count("[photo]") <= TIGHT.max_attachments
    assert "more attachment" in context.lower()


def test_a_forward_with_only_attachments_is_still_represented():
    context = summarize_message_context(
        {"fwd_messages": [{"from_id": 3, "attachments": [{"type": "story", "story": {}}]}]},
        TIGHT,
    )

    assert "[story]" in context


def test_geo_on_the_message_itself_is_reported():
    context = summarize_message_context(
        {
            "reply_message": {
                "from_id": 1,
                "geo": {"coordinates": {"latitude": 1.5, "longitude": 2.5}},
            }
        },
        TIGHT,
    )

    assert "1.5" in context
    assert "2.5" in context


def test_malformed_forward_entries_do_not_break_the_rest():
    context = summarize_message_context(
        {"fwd_messages": ["garbage", {"from_id": 1, "text": "survivor"}, None]},
        TIGHT,
    )

    assert "survivor" in context


def test_limits_reject_nonsense_configuration():
    with pytest.raises(ValueError):
        ContextLimits(max_depth=0)
    with pytest.raises(ValueError):
        ContextLimits(max_text_chars=0)
