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
