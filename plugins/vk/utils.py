"""Small shared helpers for the VK Hermes adapter."""

from __future__ import annotations

from typing import Any

DEFAULT_API_VERSION = "5.199"
DEFAULT_POLL_WAIT_SECONDS = 25
DEFAULT_MAX_MESSAGE_LENGTH = 8500
VK_API_BASE = "https://api.vk.com/method"


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
