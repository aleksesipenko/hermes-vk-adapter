"""Small shared helpers for the VK Hermes adapter."""

from __future__ import annotations

import re
from typing import Any

DEFAULT_API_VERSION = "5.199"
DEFAULT_POLL_WAIT_SECONDS = 25
DEFAULT_MAX_MESSAGE_LENGTH = 8500
VK_API_BASE = "https://api.vk.com/method"

# Longest text VK accepts in one messages.send call.
VK_MESSAGE_SEND_LIMIT = 9000
# messages.edit has its own, much smaller cap.
VK_MESSAGE_EDIT_LIMIT = 4096

MAX_ERROR_MESSAGE_CHARS = 300

# A VK community token is a long opaque run of hex.  Anything of that shape in
# text headed for a log or an error string is assumed to be a credential.
_SECRET_RUN_RE = re.compile(r"[A-Za-z0-9_\-]{32,}")


def redact_secrets(text: Any) -> str:
    """Mask token-shaped runs so credentials cannot reach logs or errors."""
    if not text:
        return ""
    return _SECRET_RUN_RE.sub("***", str(text))


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _csv_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {x.strip() for x in value.split(",") if x.strip()}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _vk_attachment_ref(kind: str, obj: dict[str, Any]) -> str | None:
    owner_id = obj.get("owner_id")
    media_id = obj.get("id")
    if owner_id is None or media_id is None:
        return None
    ref = f"{kind}{owner_id}_{media_id}"
    access_key = obj.get("access_key")
    if access_key:
        ref = f"{ref}_{access_key}"
    return ref


def _largest_photo_url(photo: dict[str, Any]) -> str | None:
    sizes = photo.get("sizes") or []
    if not isinstance(sizes, list):
        return None

    candidates: list[tuple[int, str]] = []
    for size in sizes:
        if not isinstance(size, dict) or not size.get("url"):
            continue
        candidates.append(
            (_safe_int(size.get("width"), 0) * _safe_int(size.get("height"), 0), size["url"])
        )
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


# Outbound idempotency: how many recent (peer, chunk, content) keys to remember
# and for how long a retry should resolve to the same VK random_id.
OUTBOUND_IDEMPOTENCY_MAX_ENTRIES = 512
OUTBOUND_IDEMPOTENCY_TTL_SECONDS = 120.0
