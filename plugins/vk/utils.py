"""Small shared helpers for the VK Hermes adapter."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
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

# Recently handled Long Poll events, so an in-process redelivery (a `failed`
# recovery that rewinds ts, an overlapping poll after reconnect) is suppressed.
SEEN_EVENT_MAX_ENTRIES = 2048
SEEN_EVENT_TTL_SECONDS = 600.0


def update_dedupe_key(update: dict[str, Any]) -> tuple[Any, ...] | None:
    """A stable identity for a Long Poll update, or None when it has none.

    Only VK-assigned identifiers are used. An update without one is never
    deduplicated: dropping an event we cannot identify would be worse than
    handling it twice.
    """
    if not isinstance(update, dict):
        return None
    kind = str(update.get("type") or "")
    obj = update.get("object")
    if not isinstance(obj, dict):
        return None

    if kind == "message_event":
        event_id = str(obj.get("event_id") or "")
        return ("message_event", event_id) if event_id else None

    message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    if not isinstance(message, dict):
        return None
    peer_id = _safe_int(message.get("peer_id"), 0)
    message_id = _safe_int(message.get("id"), 0)
    cmid = _safe_int(message.get("conversation_message_id"), 0)
    if not peer_id or not (message_id or cmid):
        return None
    return (kind, peer_id, message_id, cmid)

# VK accepts at most this many attachments on one messages.send call.
VK_MAX_ATTACHMENTS = 10


# VK caps a callback button payload; anything larger is not something we sent.
VK_CALLBACK_PAYLOAD_MAX_CHARS = 255


def decode_callback_payload(payload: Any) -> dict[str, Any]:
    """Decode a VK callback payload into a dict, rejecting anything odd.

    Callback payloads are attacker-influenced input: they arrive as whatever
    the VK client sends. Only a JSON object within VK's documented size cap is
    accepted; everything else decodes to an empty dict and is refused upstream.
    """
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, str) or not payload.strip():
        return {}
    if len(payload) > VK_CALLBACK_PAYLOAD_MAX_CHARS:
        return {}
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


# Who last spoke in a peer, so an interactive prompt can be bound to them.
LAST_ACTOR_MAX_ENTRIES = 256
LAST_ACTOR_TTL_SECONDS = 3600.0


# VK numbers group conversations from this offset; anything below is a DM,
# where peer_id and the user's id are the same number.
GROUP_PEER_ID_BASE = 2_000_000_000


@dataclass(frozen=True)
class ReactionConfig:
    """Numeric VK reaction ids for the message lifecycle.

    Reactions are opt-in and cosmetic. VK identifies a reaction by a numeric
    id, and the community token used here cannot call
    ``messages.getReactionsAssets`` to discover which id means what -- so the
    operator configures the exact numbers they want and nothing is guessed. An
    unset, non-numeric or non-positive value simply leaves that step off.
    """

    processing: int | None = None
    done: int | None = None
    failed: int | None = None

    @property
    def enabled(self) -> bool:
        return any((self.processing, self.done, self.failed))

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ReactionConfig:
        source = env if env is not None else os.environ
        return cls(
            processing=_reaction_id(source.get("VK_REACTION_PROCESSING_ID")),
            done=_reaction_id(source.get("VK_REACTION_DONE_ID")),
            failed=_reaction_id(source.get("VK_REACTION_FAILED_ID")),
        )


def _reaction_id(value: Any) -> int | None:
    """A configured reaction id, or None when it is absent or unusable."""
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if number > 0 else None
