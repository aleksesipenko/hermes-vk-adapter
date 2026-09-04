"""VK plain-text rendering and chunking must never lose content."""

from __future__ import annotations

import re

import pytest

from plugins.vk.formatting import chunk_vk_text, render_vk_plain_text


def visible(text: str) -> str:
    """Everything that is not whitespace, in order.

    Chunking is allowed to collapse whitespace at a seam and nothing else, so
    this is the exact invariant: no visible character may be dropped,
    duplicated, or reordered.
    """
    return re.sub(r"\s+", "", text)


def assert_reconstructs(source: str, chunks: list[str], limit: int) -> None:
    assert all(len(chunk) <= limit for chunk in chunks), "a chunk exceeded the limit"
    assert all(chunk for chunk in chunks), "an empty chunk was emitted"
    assert visible("".join(chunks)) == visible(source)


# ── chunker invariants ────────────────────────────────────────────────────


def test_short_text_is_a_single_chunk():
    assert chunk_vk_text("hello", 100) == ["hello"]


def test_empty_text_produces_no_chunks():
    assert chunk_vk_text("", 100) == []
    assert chunk_vk_text("   \n  ", 100) == []


def test_rejects_a_nonsense_limit():
    with pytest.raises(ValueError):
        chunk_vk_text("hello", 0)


def test_splits_on_readable_boundaries():
    chunks = chunk_vk_text("hello world hello world", 10)

    assert chunks == ["hello", "world", "hello", "world"]


def test_leading_whitespace_does_not_duplicate_or_drop_characters():
    """The old chunker sliced by the *stripped* length and corrupted text."""
    source = "  hello world"

    chunks = chunk_vk_text(source, 5)

    assert_reconstructs(source, chunks, 5)
    assert "".join(chunks).count("llo") == 1


@pytest.mark.parametrize("limit", [3, 5, 7, 11, 32, 100])
@pytest.mark.parametrize(
    "source",
    [
        "  indented start then a long tail of words to split apart",
        "one\n\ntwo\n\nthree\n\nfour\n\nfive",
        "  \n\n   leading blank lines\nand a line\n   indented continuation",
        "Привет мир, это проверка кириллицы и переноса длинного текста",
        "emoji 🎉🎉🎉 mixed with text 🚀 and more 🌍 content here",
        "supercalifragilisticexpialidocious" * 5,
        "trailing whitespace   \n\n   ",
        "a b c d e f g h i j k l m n o p q r s t u v w x y z",
    ],
)
def test_no_visible_character_is_ever_lost_or_duplicated(source: str, limit: int):
    assert_reconstructs(source, chunk_vk_text(source, limit), limit)


def test_long_unbroken_token_is_hard_split_without_loss():
    source = "x" * 250

    chunks = chunk_vk_text(source, 100)

    assert [len(chunk) for chunk in chunks] == [100, 100, 50]
    assert "".join(chunks) == source


def test_indentation_of_a_continuation_line_survives_a_newline_split():
    source = "first line\n    indented second line that is long"

    chunks = chunk_vk_text(source, 12)

    assert chunks[0] == "first line"
    assert chunks[1].startswith("    indented")


def test_urls_are_not_broken_when_a_boundary_exists():
    source = "see the docs: https://example.com/a/very/long/path?x=1 and then more words"

    chunks = chunk_vk_text(source, 60)

    assert any("https://example.com/a/very/long/path?x=1" in chunk for chunk in chunks)


# ── formatting feeds the chunker ──────────────────────────────────────────


def test_rendering_rewrites_a_markdown_link_and_changes_its_length():
    """Rendering changes length, so the limit must apply to rendered text."""
    source = "[docs](https://example.com/some/long/path)"

    rendered = render_vk_plain_text(source)

    assert rendered == "docs: https://example.com/some/long/path"
    assert len(rendered) != len(source)


def test_splitting_before_rendering_would_corrupt_constructs_at_a_seam():
    """Why order matters: a link or fence cut in half no longer renders.

    Rendering each half separately leaves raw Markdown in the user's message,
    which is exactly what chunk-then-format used to produce.
    """
    source = "see [the documentation](https://example.com/a/long/path) now"
    limit = 30

    split_first = [render_vk_plain_text(part) for part in chunk_vk_text(source, limit)]
    render_first = chunk_vk_text(render_vk_plain_text(source), limit)

    assert any("](" in chunk for chunk in split_first)
    assert not any("](" in chunk for chunk in render_first)

    # With room for the rendered URL, it also stays intact in one piece.
    roomy = chunk_vk_text(render_vk_plain_text(source), 40)
    assert any("https://example.com/a/long/path" in chunk for chunk in roomy)


def test_rendered_then_chunked_output_always_fits_the_limit():
    source = " ".join(f"[link{i}](https://example.com/path/number/{i})" for i in range(200))
    limit = 120

    chunks = chunk_vk_text(render_vk_plain_text(source), limit)

    assert all(len(chunk) <= limit for chunk in chunks)
    assert "https://example.com/path/number/199" in "".join(chunks)


def test_fenced_code_contents_survive_rendering_and_chunking():
    source = "before\n```python\nprint('hello')\nvalue = 1 + 2\n```\nafter"

    chunks = chunk_vk_text(render_vk_plain_text(source), 20)
    joined = "".join(chunks)

    assert "print('hello')" in joined
    assert "value = 1 + 2" in joined
    assert "```" not in joined


def test_very_long_content_is_split_and_fully_reconstructed():
    # The Cyrillic below is the point of the test, not a typo, so the
    # ambiguous-character rule is suppressed on that line.
    paragraph = "Строка с кириллицей и словами, которые нужно разбить. "  # noqa: RUF001
    source = paragraph * 260  # comfortably over 12,000 characters
    limit = 4096

    assert len(source) > 12_000
    chunks = chunk_vk_text(render_vk_plain_text(source), limit)

    assert len(chunks) > 3
    assert_reconstructs(render_vk_plain_text(source), chunks, limit)
