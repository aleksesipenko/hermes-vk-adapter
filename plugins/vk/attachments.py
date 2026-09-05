"""Bounded text summaries of VK reply, forward and attachment context.

The adapter used to hand Hermes the inbound text and the media it could
download, and discard everything else: the message being replied to, any
forwarded thread, and every attachment type it had no downloader for.  A user
forwarding a conversation and asking "what do you think?" therefore sent an
agent that could see only "what do you think?".

This module turns that context into plain text.  Every dimension is bounded --
recursion depth, message count, total characters, attachments per message --
because the input is user-supplied and ends up in an LLM prompt.  Nothing is
dropped silently: when a bound truncates something, the summary says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_SUMMARY_CHARS = 200


@dataclass(frozen=True)
class ContextLimits:
    """Hard bounds on how much quoted context one message may contribute."""

    #: All four are TOTALS for one walk, not per-message allowances. Applying
    #: max_attachments per message let three forwards of five documents emit
    #: fifteen lines under a "max_attachments=2" budget.
    max_depth: int = 3
    max_messages: int = 10
    max_text_chars: int = 2000
    max_attachments: int = 10

    def __post_init__(self) -> None:
        for name in ("max_depth", "max_messages", "max_text_chars", "max_attachments"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1, got {getattr(self, name)!r}")


def _clip(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def summarize_attachment(attachment: Any) -> str | None:
    """One bounded, human-readable line for any VK attachment.

    Types the adapter can download still get a marker so the position of the
    media inside the conversation survives; types it cannot download get their
    metadata instead of being dropped.
    """
    if not isinstance(attachment, dict):
        return None
    kind = str(attachment.get("type") or "")
    body = attachment.get(kind)
    body = body if isinstance(body, dict) else {}

    if kind == "photo":
        return "[photo]"
    if kind == "doc":
        title = _clip(body.get("title"))
        return f"[document: {title}]" if title else "[document]"
    if kind == "audio_message":
        return "[voice message]"
    if kind == "link":
        url = _clip(body.get("url"))
        title = _clip(body.get("title"))
        inner = f"{title} {url}".strip() if title else url
        return f"[link: {inner}]" if inner else "[link]"
    if kind == "sticker":
        sticker_id = body.get("sticker_id")
        return f"[sticker {sticker_id}]" if sticker_id else "[sticker]"
    if kind == "poll":
        question = _clip(body.get("question"))
        return f"[poll: {question}]" if question else "[poll]"
    if kind == "video":
        title = _clip(body.get("title"))
        return f"[video: {title}]" if title else "[video]"
    if kind == "audio":
        artist = _clip(body.get("artist"), 60)
        title = _clip(body.get("title"), 60)
        label = " - ".join(part for part in (artist, title) if part)
        return f"[audio: {label}]" if label else "[audio]"
    if kind == "wall":
        owner_id = body.get("owner_id")
        post_id = body.get("id")
        if owner_id is not None and post_id is not None:
            return f"[wall post wall{owner_id}_{post_id}]"
        return "[wall post]"
    if kind == "story":
        return "[story]"
    if not kind:
        return "[unknown attachment]"
    return f"[{kind} attachment]"


def _geo_summary(message: dict[str, Any]) -> str | None:
    """VK puts geo on the message itself rather than in ``attachments``."""
    geo = message.get("geo")
    if not isinstance(geo, dict):
        return None
    coordinates = geo.get("coordinates")
    if not isinstance(coordinates, dict):
        return "[location]"
    latitude = coordinates.get("latitude")
    longitude = coordinates.get("longitude")
    if latitude is None or longitude is None:
        return "[location]"
    return f"[location: {latitude}, {longitude}]"


class _Budget:
    """Tracks the bounds while walking a reply/forward tree."""

    def __init__(self, limits: ContextLimits) -> None:
        self.limits = limits
        self.messages = 0
        self.characters = 0
        self.attachments = 0
        self.truncated = False

    def take_message(self) -> bool:
        if self.messages >= self.limits.max_messages:
            self.truncated = True
            return False
        self.messages += 1
        return True

    def take_attachments(self, count: int) -> int:
        """How many of ``count`` attachments the remaining total budget allows."""
        allowed = max(0, self.limits.max_attachments - self.attachments)
        taken = min(count, allowed)
        if taken < count:
            self.truncated = True
        self.attachments += taken
        return taken

    def take_text(self, text: str) -> str:
        """Charge ``text`` against the total character budget."""
        remaining = self.limits.max_text_chars - self.characters
        if remaining <= 0:
            self.truncated = True
            return ""
        if len(text) > remaining:
            self.truncated = True
            text = text[:remaining]
        self.characters += len(text)
        return text


def _summarize_one(
    message: Any,
    budget: _Budget,
    depth: int,
    label: str,
    lines: list[str],
) -> None:
    if not isinstance(message, dict) or depth > budget.limits.max_depth:
        if isinstance(message, dict):
            budget.truncated = True
        return
    if not budget.take_message():
        return

    author = message.get("from_id")
    parts: list[str] = []
    text = budget.take_text(_clip(message.get("text"), budget.limits.max_text_chars))
    if text:
        parts.append(text)

    summaries: list[str] = []
    attachments = message.get("attachments")
    if isinstance(attachments, list) and attachments:
        allowed = budget.take_attachments(len(attachments))
        for attachment in attachments[:allowed]:
            summary = summarize_attachment(attachment)
            if summary:
                summaries.append(summary)
        extra = len(attachments) - allowed
        if extra > 0:
            summaries.append(f"[+{extra} more attachments]")
    geo = _geo_summary(message)
    if geo:
        summaries.append(geo)
    if summaries:
        # Attachment lines are rendered text too, so they are charged against
        # the same total character budget the message bodies use.
        rendered = budget.take_text(" ".join(summaries))
        if rendered:
            parts.append(rendered)

    if parts:
        lines.append(f"{label} from {author}: {' '.join(parts)}"[:MAX_SUMMARY_CHARS * 4])

    nested = message.get("fwd_messages")
    if isinstance(nested, list):
        for entry in nested:
            _summarize_one(entry, budget, depth + 1, "forwarded", lines)

    reply = message.get("reply_message")
    if isinstance(reply, dict):
        _summarize_one(reply, budget, depth + 1, "reply", lines)


def summarize_message_context(message: dict[str, Any], limits: ContextLimits | None = None) -> str:
    """Bounded plain-text rendering of a message's reply/forward context.

    Returns an empty string when there is no quoted context at all.
    """
    limits = limits or ContextLimits()
    if not isinstance(message, dict):
        return ""

    budget = _Budget(limits)
    lines: list[str] = []

    reply = message.get("reply_message")
    if isinstance(reply, dict):
        _summarize_one(reply, budget, 1, "reply", lines)

    forwards = message.get("fwd_messages")
    if isinstance(forwards, list):
        for entry in forwards:
            _summarize_one(entry, budget, 1, "forwarded", lines)

    if not lines:
        return ""
    if budget.truncated:
        lines.append("[context truncated]")
    return "\n".join(lines)
