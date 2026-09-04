"""VK plain-text rendering helpers."""

from __future__ import annotations

import html
import re

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_CODE_FENCE_RE = re.compile(r"```(?:[A-Za-z0-9_+.-]+)?\n?(.*?)```", re.DOTALL)


def render_vk_plain_text(content: str) -> str:
    text = str(content or "")
    text = _CODE_FENCE_RE.sub(lambda m: (m.group(1) or "").strip(), text)
    text = _MARKDOWN_LINK_RE.sub(lambda m: f"{m.group(1)}: {m.group(2)}", text)
    text = html.unescape(text)
    text = re.sub(r"</?[^>\s]+(?:\s[^>]*)?>", "", text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_\n]+)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    return text.strip()


#: Boundaries to break on, best first.  A split is only taken near the end of
#: the window so chunks stay roughly full instead of degenerating into slivers.
_SPLIT_SEPARATORS = ("\n\n", "\n", " ")
_MIN_SPLIT_RATIO = 0.4


def chunk_vk_text(text: str, limit: int) -> list[str]:
    """Split already-rendered text into chunks of at most ``limit`` characters.

    Feed this the *rendered* text: rendering can make content longer (a
    Markdown link becomes ``label: url``), so measuring the source would let a
    chunk sail past the limit once it is rendered.

    The only characters this may remove are whitespace at a seam.  Slicing is
    done by offsets into the remaining text -- never by the length of an
    already-stripped chunk, which is how the previous implementation dropped
    and duplicated characters around leading whitespace.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit!r}")
    if not text or not text.strip():
        return []

    chunks: list[str] = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining.rstrip())
            break
        split_at = _split_offset(remaining[:limit], limit)
        piece = remaining[:split_at].rstrip()
        # Not lstripped: at a newline boundary the next line's indentation is
        # content, not seam whitespace.
        remaining = remaining[split_at:]
        if piece:
            chunks.append(piece)
        elif not remaining.strip():
            break
    return [chunk for chunk in chunks if chunk]


def _split_offset(window: str, limit: int) -> int:
    """Offset just past the best separator in ``window``, else a hard split."""
    threshold = int(limit * _MIN_SPLIT_RATIO)
    for separator in _SPLIT_SEPARATORS:
        found = window.rfind(separator)
        if found > threshold:
            return found + len(separator)
    # A single unbroken token (a URL, a hash, a long word) has no readable
    # boundary; cutting it is better than dropping it.
    return limit
