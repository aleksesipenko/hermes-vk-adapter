"""Narrow raw VK API client for the Hermes VK adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .utils import (
    DEFAULT_API_VERSION,
    MAX_ERROR_MESSAGE_CHARS,
    VK_API_BASE,
    _safe_int,
    _vk_attachment_ref,
    redact_secrets,
)

logger = logging.getLogger(__name__)


def _reject_unsafe_url(url: str) -> None:
    """Refuse a URL that points at a private or internal address.

    Reuses Hermes' own URL safety check when it is importable (the gateway
    always has it). Without Hermes -- standalone tests, the doctor -- the
    scheme check below still rejects the non-HTTP targets that make SSRF
    interesting.
    """
    if not str(url or "").lower().startswith(("http://", "https://")):
        raise ValueError("VK attachment URL must be http(s)")
    try:
        from tools.url_safety import is_safe_url
    except Exception:
        return
    if not is_safe_url(url):
        raise ValueError("Blocked VK attachment URL pointing at a private address")

VK_MESSAGE_PHOTO_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}

# VK ``random_id`` is a signed 32-bit integer and must be non-zero for the
# deduplication VK performs on messages.send.
VK_RANDOM_ID_MAX = 2_147_483_647

# ── Error classification ──────────────────────────────────────────────────
#
# Deliberately small and conservative.  Only codes whose meaning is stable and
# unambiguous are listed; anything else falls through to "unknown", which is
# never auto-retried.  Getting this wrong in the permissive direction is the
# dangerous one, and even then a retry reuses the same ``random_id``, so VK
# still collapses a duplicate send.

#: VK codes worth another attempt: a transient server-side condition.
VK_RETRYABLE_ERROR_CODES = frozenset({1, 6, 10})

#: VK code -> Hermes ``SendResult.error_kind`` (gateway/platforms/base.py).
VK_ERROR_KINDS: dict[int, str] = {
    1: "transient",  # Unknown error occurred
    5: "forbidden",  # User authorization failed
    6: "rate_limited",  # Too many requests per second
    9: "rate_limited",  # Flood control
    10: "transient",  # Internal server error
    15: "forbidden",  # Access denied
    100: "unknown",  # Missing or invalid parameter
    113: "not_found",  # Invalid user id
    901: "forbidden",  # Cannot message this user without permission
    902: "forbidden",  # Blocked by the recipient's privacy settings
    914: "too_long",  # Message is too long
}


class VKApiError(RuntimeError):
    """A VK API failure with a validated code and a credential-safe message.

    The raw response is intentionally **not** retained: VK echoes the request
    back in ``error.request_params``, which carries ``access_token`` and the
    outgoing message body.  Keeping it would put both into any log line that
    formats the exception.
    """

    def __init__(self, method: str, payload: Any):
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            error = {}
        self.method = str(method)
        self.code: int = _safe_int(error.get("error_code"), 0)
        self.message: str = redact_secrets(error.get("error_msg") or "")[:MAX_ERROR_MESSAGE_CHARS]
        super().__init__(f"VK API error in {self.method}: {self.code} {self.message}")

    @property
    def retryable(self) -> bool:
        return self.code in VK_RETRYABLE_ERROR_CODES


def is_transient_transport_error(exc: BaseException) -> bool:
    """Whether ``exc`` is a connection-level failure worth one more attempt."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


def classify_vk_error_kind(exc: BaseException) -> str:
    """Map a failure to a Hermes ``SendResult.error_kind`` value."""
    if isinstance(exc, VKApiError):
        return VK_ERROR_KINDS.get(exc.code, "unknown")
    if is_transient_transport_error(exc):
        return "transient"
    return "unknown"


def is_retryable(exc: BaseException) -> bool:
    """Whether re-issuing the identical request is worth trying."""
    if isinstance(exc, VKApiError):
        return exc.retryable
    return is_transient_transport_error(exc)


@dataclass
class LongPollState:
    server: str
    key: str
    ts: str


class VKRestClient:
    #: Bounded backoff for the few calls that are safe to repeat.
    RETRY_BASE_DELAY = 0.5
    RETRY_MAX_DELAY = 30.0
    DEFAULT_MAX_ATTEMPTS = 3

    def __init__(
        self,
        token: str,
        group_id: int,
        api_version: str,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.token = token
        self.group_id = group_id
        self.api_version = api_version or DEFAULT_API_VERSION
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0))
        # Injected so backoff is exercised in tests without real sleeping and
        # jitter is reproducible.
        self._sleep = sleep
        self._rng = rng or random.Random()

    async def close(self) -> None:
        await self.http.aclose()

    def new_random_id(self) -> int:
        """A fresh non-zero VK ``random_id``."""
        return self._rng.randint(1, VK_RANDOM_ID_MAX)

    async def call(self, method: str, **params: Any) -> Any:
        """Issue one VK API call. Never retried -- callers decide.

        Most VK methods are not safe to repeat blindly (uploads attach twice,
        edits race, callback answers are once-per-event), so retrying is opt-in
        via :meth:`call_idempotent` rather than the default.
        """
        response = await self.http.post(
            f"{VK_API_BASE}/{method}",
            data={"access_token": self.token, "v": self.api_version, **params},
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise VKApiError(method, payload)
        return payload.get("response")

    async def call_idempotent(self, method: str, *, max_attempts: int | None = None, **params: Any):
        """Issue a call whose parameters make a repeat safe, with backoff.

        The caller must have pinned every field that makes the request unique
        (for messages.send that is ``random_id``) *before* the first attempt, so
        a retry after a timeout cannot become a second logical message.
        """
        attempts = max(1, int(max_attempts or self.DEFAULT_MAX_ATTEMPTS))
        for attempt in range(1, attempts + 1):
            try:
                return await self.call(method, **params)
            except Exception as exc:
                if attempt >= attempts or not is_retryable(exc):
                    raise
                delay = self._backoff_delay(attempt)
                logger.info(
                    "VK %s attempt %d/%d failed (%s); retrying in %.2fs",
                    method,
                    attempt,
                    attempts,
                    redact_secrets(exc),
                    delay,
                )
                await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff_delay(self, attempt: int) -> float:
        base = min(self.RETRY_BASE_DELAY * (2 ** (attempt - 1)), self.RETRY_MAX_DELAY)
        return min(base * (1.0 + self._rng.random() * 0.25), self.RETRY_MAX_DELAY)

    async def get_long_poll_state(self) -> LongPollState:
        response = await self.call("groups.getLongPollServer", group_id=self.group_id)
        return LongPollState(server=response["server"], key=response["key"], ts=str(response["ts"]))

    async def poll(self, state: LongPollState, wait_seconds: int) -> dict[str, Any]:
        response = await self.http.get(
            state.server,
            params={"act": "a_check", "key": state.key, "ts": state.ts, "wait": wait_seconds},
        )
        response.raise_for_status()
        return response.json()

    async def send_message(
        self,
        *,
        peer_id: int,
        message: str = "",
        attachment: str | None = None,
        reply_to: str | int | None = None,
        keyboard: str | None = None,
        sticker_id: int | None = None,
        random_id: int | None = None,
        max_attempts: int | None = None,
    ) -> Any:
        """Send one VK message.

        ``random_id`` is VK's idempotency key: the caller pins it so that a
        retry after a timeout -- where VK may well have accepted the first
        attempt -- resolves to the same logical message instead of a duplicate.
        """
        params: dict[str, Any] = {
            "peer_id": peer_id,
            "random_id": int(random_id) if random_id else self.new_random_id(),
        }
        if message:
            params["message"] = message
        if attachment:
            params["attachment"] = attachment
        if reply_to:
            params["reply_to"] = int(reply_to)
        if keyboard:
            params["keyboard"] = keyboard
        if sticker_id:
            params["sticker_id"] = int(sticker_id)
        return await self.call_idempotent("messages.send", max_attempts=max_attempts, **params)

    async def edit_message(
        self,
        *,
        peer_id: int,
        message: str,
        message_id: int | None = None,
        cmid: int | None = None,
        keyboard: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {"peer_id": peer_id, "message": message}
        if message_id:
            params["message_id"] = int(message_id)
        if cmid:
            params["cmid"] = int(cmid)
        if keyboard:
            params["keyboard"] = keyboard
        return await self.call("messages.edit", **params)

    async def send_message_event_answer(
        self,
        *,
        event_id: str,
        user_id: int,
        peer_id: int,
        text: str,
    ) -> Any:
        event_data = json.dumps(
            {"type": "show_snackbar", "text": text[:90]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return await self.call(
            "messages.sendMessageEventAnswer",
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data=event_data,
        )

    async def set_typing(self, peer_id: int) -> None:
        try:
            await self.call(
                "messages.setActivity", peer_id=peer_id, type="typing", group_id=self.group_id
            )
        except Exception as exc:
            logger.debug("VK typing indicator failed for peer_id=%s: %s", peer_id, exc)

    async def download_bytes(self, url: str, *, max_bytes: int = 80 * 1024 * 1024) -> bytes:
        """Stream an attachment, stopping the moment it exceeds ``max_bytes``.

        The cap is enforced *during* the stream, so a caller passing its
        remaining budget can never be overshot by a large body.

        Attachment URLs come out of the VK event payload, so the same
        SSRF protections Hermes applies to its own media downloads are applied
        here: the target is pre-flighted and every redirect hop is re-validated.
        """
        if max_bytes <= 0:
            raise ValueError("VK attachment download budget is exhausted")
        _reject_unsafe_url(url)
        async with self.http.stream("GET", url, follow_redirects=True) as response:
            _reject_unsafe_url(str(response.url))
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"VK attachment exceeds max_bytes={max_bytes}")
                chunks.append(chunk)
            return b"".join(chunks)

    async def upload_document_raw(
        self, *, peer_id: int, path: str, title: str | None = None
    ) -> str:
        upload_server = await self.call("docs.getMessagesUploadServer", type="doc", peer_id=peer_id)
        file_path = Path(path)
        upload_name = title or file_path.name
        mime_type = mimetypes.guess_type(upload_name)[0] or "application/octet-stream"
        payload = await asyncio.to_thread(file_path.read_bytes)
        upload_response = await self.http.post(
            upload_server["upload_url"],
            files={"file": (upload_name, payload, mime_type)},
        )
        upload_response.raise_for_status()
        uploaded = upload_response.json()
        file_token = uploaded.get("file")
        if not file_token:
            raise RuntimeError(f"VK upload did not return file token: {uploaded}")

        saved = await self.call("docs.save", file=file_token, title=upload_name)
        doc = saved.get("doc") if isinstance(saved, dict) else None
        if not isinstance(doc, dict):
            raise RuntimeError(f"Unexpected docs.save response shape: {saved}")
        ref = _vk_attachment_ref("doc", doc)
        if not ref:
            raise RuntimeError(f"Cannot build VK doc attachment ref from: {doc}")
        return ref

    async def upload_photo_message_raw(self, *, peer_id: int, path: str) -> str:
        upload_server = await self.call("photos.getMessagesUploadServer", peer_id=peer_id)
        file_path = Path(path)
        upload_name = file_path.name
        mime_type = VK_MESSAGE_PHOTO_MIME_TYPES.get(file_path.suffix.lower())
        if mime_type is None:
            mime_type = mimetypes.guess_type(upload_name)[0] or "application/octet-stream"
        payload = await asyncio.to_thread(file_path.read_bytes)
        upload_response = await self.http.post(
            upload_server["upload_url"],
            files={"photo": (upload_name, payload, mime_type)},
        )
        upload_response.raise_for_status()
        uploaded = upload_response.json()
        photo = uploaded.get("photo")
        server = uploaded.get("server")
        upload_hash = uploaded.get("hash")
        if not photo or server is None or not upload_hash:
            raise RuntimeError(f"VK photo upload did not return save parameters: {uploaded}")

        saved = await self.call(
            "photos.saveMessagesPhoto", photo=photo, server=server, hash=upload_hash
        )
        photo_obj = saved[0] if isinstance(saved, list) and saved else None
        if not isinstance(photo_obj, dict):
            raise RuntimeError(f"Unexpected photos.saveMessagesPhoto response shape: {saved}")
        ref = _vk_attachment_ref("photo", photo_obj)
        if not ref:
            raise RuntimeError(f"Cannot build VK photo attachment ref from: {photo_obj}")
        return ref

    async def delete_message(
        self,
        *,
        peer_id: int,
        message_ids: int | None = None,
        cmids: int | None = None,
        delete_for_all: bool = True,
    ) -> Any:
        """Delete one of our own messages. Never retried: a repeat is unclear."""
        params: dict[str, Any] = {"peer_id": peer_id, "delete_for_all": 1 if delete_for_all else 0}
        if message_ids:
            params["message_ids"] = int(message_ids)
        if cmids:
            params["cmids"] = int(cmids)
        return await self.call("messages.delete", **params)

    async def mark_as_read(self, *, peer_id: int, start_message_id: int | None = None) -> Any:
        params: dict[str, Any] = {"peer_id": peer_id, "group_id": self.group_id}
        if start_message_id:
            params["start_message_id"] = int(start_message_id)
        return await self.call("messages.markAsRead", **params)

    async def send_reaction(self, *, peer_id: int, cmid: int, reaction_id: int) -> Any:
        """Add a reaction. VK keys reactions on cmid, not the global id."""
        return await self.call(
            "messages.sendReaction",
            peer_id=peer_id,
            cmid=int(cmid),
            reaction_id=int(reaction_id),
        )

    async def delete_reaction(self, *, peer_id: int, cmid: int) -> Any:
        return await self.call("messages.deleteReaction", peer_id=peer_id, cmid=int(cmid))

    async def get_users(self, user_ids: list[int] | int) -> list[dict[str, Any]]:
        """Resolve VK user names. Narrowest method a community token can use."""
        ids = user_ids if isinstance(user_ids, list) else [user_ids]
        response = await self.call("users.get", user_ids=",".join(str(i) for i in ids))
        return response if isinstance(response, list) else []

    async def get_conversation(self, peer_id: int) -> dict[str, Any]:
        """Fetch one conversation's metadata (title for a group chat)."""
        response = await self.call(
            "messages.getConversationsById", peer_ids=int(peer_id), group_id=self.group_id
        )
        return response if isinstance(response, dict) else {}
