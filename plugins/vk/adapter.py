"""VK community Long Poll platform adapter for Hermes Agent."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import random
import time
from typing import Any

import httpx
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    SUPPORTED_DOCUMENT_TYPES,
    SUPPORTED_IMAGE_DOCUMENT_TYPES,
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_audio_from_url,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_image_from_url,
)

from .callbacks import VKCallbackRouter
from .client import (
    VK_MESSAGE_PHOTO_MIME_TYPES,
    LongPollState,
    VKApiError,
    VKRestClient,
    classify_vk_error_kind,
    is_retryable,
)
from .formatting import render_vk_plain_text
from .keyboards import VKKeyboardFactory, model_picker_provider_text
from .state import BoundedTTLCache
from .utils import (
    DEFAULT_API_VERSION,
    DEFAULT_MAX_MESSAGE_LENGTH,
    DEFAULT_POLL_WAIT_SECONDS,
    OUTBOUND_IDEMPOTENCY_MAX_ENTRIES,
    OUTBOUND_IDEMPOTENCY_TTL_SECONDS,
    SEEN_EVENT_MAX_ENTRIES,
    SEEN_EVENT_TTL_SECONDS,
    _csv_set,
    _largest_photo_url,
    _safe_int,
    _truthy,
    redact_secrets,
    update_dedupe_key,
)

logger = logging.getLogger(__name__)


class VKAdapter(BasePlatformAdapter):
    #: Bounded backoff between failed Long Poll attempts.
    POLL_BACKOFF_BASE = 1.0
    POLL_BACKOFF_MAX = 60.0

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("vk"))
        extra = getattr(config, "extra", {}) or {}
        self.token = (
            os.getenv("VK_GROUP_TOKEN") or getattr(config, "token", "") or extra.get("token", "")
        )
        self.group_id = _safe_int(os.getenv("VK_GROUP_ID") or extra.get("group_id"), 0)
        self.api_version = (
            os.getenv("VK_API_VERSION") or extra.get("api_version") or DEFAULT_API_VERSION
        )
        self.wait_seconds = _safe_int(
            os.getenv("VK_POLL_WAIT_SECONDS") or extra.get("poll_wait_seconds"),
            DEFAULT_POLL_WAIT_SECONDS,
        )
        self.max_message_length = _safe_int(
            os.getenv("VK_MAX_MESSAGE_LENGTH") or extra.get("max_message_length"),
            DEFAULT_MAX_MESSAGE_LENGTH,
        )
        self.require_mention = _truthy(
            os.getenv("VK_REQUIRE_MENTION") or extra.get("require_mention")
        )
        self.command_keyboard_enabled = not _truthy(
            os.getenv("VK_COMMAND_KEYBOARD_DISABLED")
            or (str(os.getenv("VK_COMMAND_KEYBOARD", "")).lower() == "false")
        )
        self.debug_updates = _truthy(os.getenv("VK_DEBUG_UPDATES"))
        self.allowed_users = _csv_set(os.getenv("VK_ALLOWED_USERS"))
        self.allow_all_users = _truthy(os.getenv("VK_ALLOW_ALL_USERS"))
        self.client: VKRestClient | None = None
        self.poll_task: asyncio.Task | None = None
        self.longpoll_state: LongPollState | None = None
        self._approval_counter = 0
        self._approval_state: dict[int, str] = {}
        self._slash_confirm_state: dict[str, str] = {}
        self._clarify_state: dict[str, str] = {}
        self._model_picker_state: dict[str, dict[str, Any]] = {}
        self._keyboards = VKKeyboardFactory()
        self._outbound_random_ids = BoundedTTLCache(
            max_entries=OUTBOUND_IDEMPOTENCY_MAX_ENTRIES,
            ttl_seconds=OUTBOUND_IDEMPOTENCY_TTL_SECONDS,
        )
        self._seen_events = BoundedTTLCache(
            max_entries=SEEN_EVENT_MAX_ENTRIES,
            ttl_seconds=SEEN_EVENT_TTL_SECONDS,
        )
        # Long Poll health, reported truthfully rather than assumed.
        self._poll_failures = 0
        self._last_successful_poll: float | None = None
        self._longpoll_degraded = False
        self._poll_sleep = asyncio.sleep

    def _longpoll_lock_identity(self) -> str:
        """A stable, non-secret identity for the scoped Long Poll lock.

        VK Bots Long Poll is per-community: two gateways consuming the same
        community both receive every event and both answer, so the community is
        what must be owned exclusively.  Keying on the group id alone (rather
        than on the token) also means rotating the token while an old gateway
        is still running is still detected as a conflict instead of silently
        starting a second consumer.  A short token fingerprint is logged
        separately for diagnostics; the token itself never appears anywhere.
        """
        return f"vk-group-{self.group_id}"

    def _token_fingerprint(self) -> str:
        """Non-reversible token marker, safe for logs and diagnostics."""
        if not self.token:
            return "unset"
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:12]

    def _build_client(self) -> VKRestClient:
        return VKRestClient(self.token, self.group_id, self.api_version)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.token or not self.group_id:
            self._set_fatal_error(
                "vk_config_missing",
                "VK_GROUP_TOKEN and VK_GROUP_ID are required",
                retryable=False,
            )
            return False

        if not self._acquire_platform_lock(
            "vk-longpoll", self._longpoll_lock_identity(), "VK community Long Poll"
        ):
            return False

        client = self._build_client()
        try:
            self.longpoll_state = await client.get_long_poll_state()
        except Exception as exc:
            # Every failed-connect path must give the lock back, or a retry
            # would deadlock against this same process.
            await self._close_client(client)
            self._release_platform_lock()
            self._set_fatal_error(
                "vk_longpoll_unavailable",
                f"Cannot open VK Long Poll: {redact_secrets(exc)}",
                retryable=is_retryable(exc) or not isinstance(exc, VKApiError),
            )
            return False

        self.client = client
        self._poll_failures = 0
        self._longpoll_degraded = False
        self._last_successful_poll = None
        self._mark_connected()
        self.poll_task = asyncio.create_task(self._poll_loop(), name="hermes-vk-longpoll")
        self.poll_task.add_done_callback(self._on_poll_task_done)
        logger.info(
            "VK adapter connected: group_id=%s api_version=%s token=%s reconnect=%s",
            self.group_id,
            self.api_version,
            self._token_fingerprint(),
            is_reconnect,
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        task = self.poll_task
        if task and not task.done():
            task.remove_done_callback(self._on_poll_task_done)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("VK Long Poll task ended with an error during shutdown")
        self.poll_task = None
        await self._close_client(self.client)
        self.client = None
        self._release_platform_lock()

    @staticmethod
    async def _close_client(client: VKRestClient | None) -> None:
        if client is None:
            return
        try:
            await client.close()
        except Exception as exc:
            logger.debug("VK client close failed: %s", redact_secrets(exc))

    def _on_poll_task_done(self, task: asyncio.Task) -> None:
        """A dead poll loop must never look healthy."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.error("VK Long Poll task terminated: %s", redact_secrets(exc))
        self._set_fatal_error(
            "vk_longpoll_task_failed",
            f"VK Long Poll task stopped: {redact_secrets(exc)}",
            retryable=True,
        )

    async def send_typing(self, chat_id: str, metadata: dict[str, Any] | None = None) -> None:
        if not self.client:
            return
        peer_id = _safe_int(chat_id)
        if peer_id:
            await self.client.set_typing(peer_id)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if not self.client:
            return SendResult(success=False, error="VK adapter is not connected", retryable=True)
        peer_id = _safe_int(chat_id)
        if not peer_id:
            return SendResult(success=False, error=f"Invalid VK peer_id: {chat_id!r}")

        try:
            chunks = self._chunk_text(content or "") or [""]
            sent_ids: list[str] = []
            for index, chunk in enumerate(chunks):
                rendered = self.format_message(chunk)
                current_reply_to = reply_to if index == 0 else None
                first_chunk = index == 0
                keyboard = (
                    self._command_keyboard()
                    if self.command_keyboard_enabled and first_chunk
                    else None
                )
                random_id = self._chunk_random_id(peer_id, index, rendered)
                try:
                    response = await self.client.send_message(
                        peer_id=peer_id,
                        message=rendered,
                        reply_to=current_reply_to,
                        keyboard=keyboard,
                        random_id=random_id,
                    )
                except VKApiError as exc:
                    if not current_reply_to or "reply_to" not in str(exc):
                        raise
                    logger.info(
                        "VK reply_to rejected for peer_id=%s; retrying without reply_to", peer_id
                    )
                    # VK rejected the request outright, so nothing was
                    # delivered: the same random_id is still the right key.
                    response = await self.client.send_message(
                        peer_id=peer_id,
                        message=rendered,
                        keyboard=keyboard,
                        random_id=random_id,
                    )
                sent_ids.append(str(response))
            return SendResult(
                success=True,
                message_id=sent_ids[-1] if sent_ids else None,
                continuation_message_ids=tuple(sent_ids[:-1]),
            )
        except Exception as exc:
            logger.exception("VK send failed peer_id=%s", peer_id)
            return self._failed_send(exc)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: str | None = None,
        file_name: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        file_names = [file_name] if file_name else None
        return await self.send_media_files(
            chat_id,
            [file_path],
            caption or "",
            file_names=file_names,
            reply_to=reply_to,
            metadata=metadata,
            **kwargs,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        if not self.client:
            return SendResult(success=False, error="VK adapter is not connected", retryable=True)
        peer_id = _safe_int(chat_id)
        if not peer_id:
            return SendResult(success=False, error=f"Invalid VK peer_id: {chat_id!r}")
        if not await asyncio.to_thread(os.path.exists, image_path):
            return SendResult(success=False, error=f"VK image path does not exist: {image_path}")
        if os.path.splitext(image_path)[1].lower() not in VK_MESSAGE_PHOTO_MIME_TYPES:
            return await self.send_document(
                chat_id=chat_id,
                file_path=image_path,
                caption=caption,
                file_name=os.path.basename(image_path),
                reply_to=reply_to,
                metadata=metadata,
                **kwargs,
            )
        try:
            ref = await self.client.upload_photo_message_raw(peer_id=peer_id, path=image_path)
            send_kwargs: dict[str, Any] = {
                "peer_id": peer_id,
                "message": caption or "",
                "attachment": ref,
            }
            if reply_to:
                send_kwargs["reply_to"] = reply_to
            try:
                response = await self.client.send_message(**send_kwargs)
            except VKApiError as exc:
                if not reply_to or "reply_to" not in str(exc):
                    raise
                logger.info(
                    "VK photo reply_to rejected for peer_id=%s; retrying without reply_to", peer_id
                )
                response = await self.client.send_message(
                    peer_id=peer_id,
                    message=caption or "",
                    attachment=ref,
                )
            return SendResult(success=True, message_id=str(response))
        except (VKApiError, RuntimeError, OSError, ValueError, httpx.HTTPError) as exc:
            logger.warning(
                "VK native photo send failed peer_id=%s; trying document fallback: %s",
                peer_id,
                exc,
            )
            return await self.send_document(
                chat_id=chat_id,
                file_path=image_path,
                caption=caption,
                file_name=os.path.basename(image_path),
                reply_to=reply_to,
                metadata=metadata,
                **kwargs,
            )

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        peer_id = _safe_int(chat_id)
        chat_type = "group" if peer_id >= 2_000_000_000 else "dm"
        return {
            "id": chat_id,
            "chat_id": chat_id,
            "name": f"VK peer {chat_id}",
            "type": chat_type,
        }

    async def send_media_files(
        self,
        chat_id: str,
        media_files: list[str],
        caption: str = "",
        *,
        file_names: list[str | None] | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        if not self.client:
            return SendResult(success=False, error="VK adapter is not connected", retryable=True)
        peer_id = _safe_int(chat_id)
        if not peer_id:
            return SendResult(success=False, error=f"Invalid VK peer_id: {chat_id!r}")
        for file_path in media_files:
            if not await asyncio.to_thread(os.path.exists, file_path):
                return SendResult(
                    success=False, error=f"VK document path does not exist: {file_path}"
                )
        try:
            refs = []
            for index, file_path in enumerate(media_files):
                title = file_names[index] if file_names and index < len(file_names) else None
                refs.append(
                    await self.client.upload_document_raw(
                        peer_id=peer_id, path=file_path, title=title
                    )
                )
            try:
                send_kwargs: dict[str, Any] = {
                    "peer_id": peer_id,
                    "message": caption,
                    "attachment": ",".join(refs),
                }
                if reply_to:
                    send_kwargs["reply_to"] = reply_to
                response = await self.client.send_message(**send_kwargs)
            except VKApiError as exc:
                if not reply_to or "reply_to" not in str(exc):
                    raise
                logger.info(
                    "VK media reply_to rejected for peer_id=%s; retrying without reply_to", peer_id
                )
                response = await self.client.send_message(
                    peer_id=peer_id,
                    message=caption,
                    attachment=",".join(refs),
                )
            return SendResult(success=True, message_id=str(response))
        except VKApiError as exc:
            error = self._media_vk_api_error(exc)
            if error == str(exc):
                logger.exception("VK media send failed peer_id=%s", peer_id)
            else:
                logger.warning("VK media send failed peer_id=%s: %s", peer_id, error)
            return self._failed_send(exc, error=error)
        except Exception as exc:
            logger.exception("VK media send failed peer_id=%s", peer_id)
            return self._failed_send(exc)

    @staticmethod
    def _media_vk_api_error(exc: VKApiError) -> str:
        if exc.method == "docs.getMessagesUploadServer" and exc.code == 15:
            return (
                "VK document upload denied: VK_GROUP_TOKEN does not have the docs permission. "
                'Create a VK community token with messages/docs rights and update VK_GROUP_TOKEN. '
                'See README section "Настройка VK".'
            )
        return str(exc)

    async def _poll_loop(self) -> None:
        assert self.client is not None
        while self._running:
            try:
                if self.longpoll_state is None:
                    self.longpoll_state = await self.client.get_long_poll_state()
                payload = await self.client.poll(self.longpoll_state, self.wait_seconds)
                if self.debug_updates:
                    logger.info(
                        "VK Long Poll payload: %s",
                        json.dumps(payload, ensure_ascii=False)[:4000],
                    )
                # A response arrived: the transport is healthy again, whatever
                # the response says.
                self._note_poll_success()

                if payload.get("failed"):
                    if not await self._handle_longpoll_failed(payload):
                        return
                    continue
                if "ts" in payload:
                    self.longpoll_state.ts = str(payload["ts"])
                await self._dispatch_updates(payload.get("updates") or [])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._note_poll_failure(exc)

    async def _dispatch_updates(self, updates: list[Any]) -> None:
        """Dispatch each update independently.

        One malformed or handler-crashing update must not cost us the rest of
        the batch -- they are unrelated messages that happened to share a poll
        response.
        """
        for update in updates:
            if not isinstance(update, dict):
                logger.debug("VK update ignored: not an object")
                continue
            if self._is_duplicate_update(update):
                logger.debug("VK duplicate update suppressed: type=%s", update.get("type"))
                continue
            try:
                await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "VK update handling failed: type=%s: %s",
                    update.get("type"),
                    redact_secrets(exc),
                )

    def _is_duplicate_update(self, update: dict[str, Any]) -> bool:
        """Suppress an update already handled recently.

        Bounded and in-memory on purpose: this covers redelivery inside one
        gateway process (a `failed` recovery that rewinds `ts`, an overlapping
        poll after a reconnect).  Recovering a backlog across a long outage
        would need durable storage and is explicitly out of scope.
        """
        key = update_dedupe_key(update)
        if key is None:
            return False
        if key in self._seen_events:
            return True
        self._seen_events.set(key, True)
        return False

    def _note_poll_success(self) -> None:
        self._last_successful_poll = time.monotonic()
        self._poll_failures = 0
        if self._longpoll_degraded:
            self._longpoll_degraded = False
            logger.info("VK Long Poll recovered")
            self._mark_connected()

    async def _note_poll_failure(self, exc: BaseException) -> None:
        self._poll_failures += 1
        logger.warning(
            "VK Long Poll failure %d: %s", self._poll_failures, redact_secrets(exc)
        )
        if not self._longpoll_degraded:
            # Report degraded once per outage instead of on every retry, so the
            # status file reflects state rather than noise.
            self._longpoll_degraded = True
            self._write_runtime_status_safe(
                "longpoll_degraded",
                platform_state="retrying",
                error_code="vk_longpoll_unreachable",
                error_message=redact_secrets(exc)[:200],
            )
        await self._poll_sleep(self._poll_backoff_delay())

    def _poll_backoff_delay(self) -> float:
        exponent = max(0, self._poll_failures - 1)
        base = min(self.POLL_BACKOFF_BASE * (2**exponent), self.POLL_BACKOFF_MAX)
        return min(base * (1.0 + random.random() * 0.25), self.POLL_BACKOFF_MAX)

    async def _handle_longpoll_failed(self, payload: dict[str, Any]) -> bool:
        """Apply the documented recovery for a Long Poll ``failed`` response.

        Returns False when the loop must stop instead of retrying.

          1  history is outdated -> keep the key, continue from the returned ts
          2  key expired         -> request a new key, keep our ts
          3  information lost    -> request a new key *and* ts
          4  unsupported version -> configuration error; retrying cannot help
        """
        assert self.client is not None
        failed = _safe_int(payload.get("failed"), 0)

        if failed == 1:
            if self.longpoll_state is not None:
                self.longpoll_state.ts = str(payload.get("ts", self.longpoll_state.ts))
            return True

        if failed == 4:
            self._set_fatal_error(
                "vk_longpoll_version",
                (
                    "VK rejected the Long Poll API version. Set VK_API_VERSION to a "
                    "version your community's Long Poll settings support."
                ),
                retryable=False,
            )
            return False

        if failed in (2, 3):
            previous_ts = self.longpoll_state.ts if self.longpoll_state else None
            refreshed = await self.client.get_long_poll_state()
            # failed=2 is only an expired key: our position in the stream is
            # still valid, and taking VK's fresh ts would skip everything that
            # arrived in between.
            if failed == 2 and previous_ts is not None:
                refreshed.ts = previous_ts
            self.longpoll_state = refreshed
            logger.info("VK Long Poll state refreshed after failed=%s", failed)
            return True

        logger.warning("VK Long Poll returned unknown failed=%s; refreshing state", failed)
        self.longpoll_state = await self.client.get_long_poll_state()
        return True

    async def _handle_update(self, update: dict[str, Any]) -> None:
        if update.get("type") != "message_new":
            if update.get("type") == "message_event":
                await self._handle_message_event(update.get("object") or {})
                return
            logger.debug("VK update ignored: type=%s", update.get("type"))
            return
        message = (update.get("object") or {}).get("message") or {}
        await self._handle_message_new(message, update)

    async def _handle_message_new(
        self, message: dict[str, Any], raw_update: dict[str, Any]
    ) -> None:
        peer_id = _safe_int(message.get("peer_id"), 0)
        from_id = _safe_int(message.get("from_id"), 0)
        text = str(message.get("text") or "").strip()
        chat_type = "group" if peer_id >= 2_000_000_000 else "dm"
        msg_id_value = (
            message.get("conversation_message_id")
            if chat_type == "group"
            else message.get("id") or message.get("conversation_message_id")
        )
        msg_id = str(msg_id_value or "")
        if not peer_id or not from_id:
            return
        if not self._is_allowed_vk_user(from_id):
            logger.info(
                "VK user denied by local allowlist: from_id=%s peer_id=%s", from_id, peer_id
            )
            return

        if (
            chat_type == "group"
            and self.require_mention
            and not self._is_group_activation(text, message)
        ):
            return

        media_paths, media_types, inferred_type = await self._extract_media(message)
        if text.startswith("/"):
            inferred_type = MessageType.COMMAND
        if not text and media_paths:
            text = self._default_media_text(inferred_type)
        if not text and not media_paths:
            return

        source = self.build_source(
            chat_id=str(peer_id),
            chat_name=f"VK peer {peer_id}",
            chat_type=chat_type,
            user_id=str(from_id),
            user_name=f"VK user {from_id}",
        )
        event = MessageEvent(
            text=text,
            message_type=inferred_type,
            source=source,
            raw_message=raw_update,
            message_id=msg_id,
            media_urls=media_paths,
            media_types=media_types,
            reply_to_message_id=str((message.get("reply_message") or {}).get("id") or "") or None,
            reply_to_text=(message.get("reply_message") or {}).get("text"),
        )
        await self.handle_message(event)

    async def _handle_message_event(self, event: dict[str, Any]) -> None:
        if not self.client:
            return
        router = VKCallbackRouter(
            client=self.client,
            is_allowed_user=self._is_allowed_vk_user,
            keyboards=self._keyboard_factory(),
            command_keyboard_enabled=getattr(self, "command_keyboard_enabled", True),
            approval_state=self._state_dict("_approval_state"),
            slash_confirm_state=self._state_dict("_slash_confirm_state"),
            clarify_state=self._state_dict("_clarify_state"),
            model_picker_state=self._state_dict("_model_picker_state"),
        )
        await router.handle(event)

    def _state_dict(self, name: str) -> dict:
        state = getattr(self, name, None)
        if state is None:
            state = {}
            setattr(self, name, state)
        return state

    async def _extract_media(
        self, message: dict[str, Any]
    ) -> tuple[list[str], list[str], MessageType]:
        media_paths: list[str] = []
        media_types: list[str] = []
        inferred = MessageType.TEXT
        attachments = message.get("attachments") or []
        if not isinstance(attachments, list):
            return media_paths, media_types, inferred

        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            kind = attachment.get("type")
            try:
                if kind == "photo":
                    url = _largest_photo_url(attachment.get("photo") or {})
                    if url:
                        media_paths.append(await cache_image_from_url(url, ext=".jpg"))
                        media_types.append("image/jpeg")
                        inferred = MessageType.PHOTO
                elif kind == "doc":
                    doc = attachment.get("doc") or {}
                    url = doc.get("url")
                    title = doc.get("title") or f"vk_doc_{doc.get('id', 'unknown')}"
                    if url:
                        assert self.client is not None
                        data = await self.client.download_bytes(url)
                        raw_ext = str(doc.get("ext") or "").strip().lower()
                        ext = f".{raw_ext}" if raw_ext and not raw_ext.startswith(".") else raw_ext
                        if not ext:
                            _, ext = os.path.splitext(title)
                            ext = ext.lower()
                        mime_type = (
                            SUPPORTED_IMAGE_DOCUMENT_TYPES.get(ext)
                            or SUPPORTED_DOCUMENT_TYPES.get(ext)
                            or mimetypes.guess_type(title)[0]
                            or "application/octet-stream"
                        )
                        if ext in SUPPORTED_IMAGE_DOCUMENT_TYPES or mime_type.startswith("image/"):
                            image_ext = ext if ext in SUPPORTED_IMAGE_DOCUMENT_TYPES else ".jpg"
                            media_paths.append(cache_image_from_bytes(data, image_ext))
                            media_types.append(
                                mime_type if mime_type.startswith("image/") else "image/jpeg"
                            )
                            inferred = MessageType.PHOTO
                        else:
                            media_paths.append(cache_document_from_bytes(data, title))
                            media_types.append(mime_type)
                            inferred = MessageType.DOCUMENT
                elif kind == "audio_message":
                    audio = attachment.get("audio_message") or {}
                    url = audio.get("link_ogg") or audio.get("link_mp3")
                    if url:
                        ext = ".ogg" if audio.get("link_ogg") else ".mp3"
                        media_paths.append(await cache_audio_from_url(url, ext=ext))
                        media_types.append("audio/ogg" if ext == ".ogg" else "audio/mpeg")
                        inferred = MessageType.VOICE
            except Exception as exc:
                logger.warning("VK attachment cache failed kind=%s: %s", kind, exc)
        return media_paths, media_types, inferred

    def _default_media_text(self, message_type: MessageType) -> str:
        if message_type == MessageType.PHOTO:
            return "[VK photo attachment]"
        if message_type == MessageType.VOICE:
            return "[VK voice message attachment]"
        if message_type == MessageType.DOCUMENT:
            return "[VK document attachment]"
        return "[VK attachment]"

    def _is_allowed_vk_user(self, from_id: int) -> bool:
        if self.allow_all_users:
            return True
        if self.allowed_users:
            return str(from_id) in self.allowed_users
        return True

    def _is_group_activation(self, text: str, message: dict[str, Any] | None = None) -> bool:
        if not text:
            return self._is_reply_to_bot(message)
        lowered = text.lower()
        return (
            lowered.startswith("/")
            or f"[club{self.group_id}|" in lowered
            or f"@club{self.group_id}" in lowered
            or "hermes" in lowered
            or self._is_reply_to_bot(message)
        )

    def _is_reply_to_bot(self, message: dict[str, Any] | None) -> bool:
        if not isinstance(message, dict):
            return False
        reply = message.get("reply_message") or {}
        if not isinstance(reply, dict):
            return False
        return _safe_int(reply.get("from_id"), 0) == -abs(self.group_id) or bool(reply.get("out"))

    def format_message(self, content: str) -> str:
        return render_vk_plain_text(content)

    def _keyboard_factory(self) -> VKKeyboardFactory:
        factory = getattr(self, "_keyboards", None)
        if factory is None:
            factory = VKKeyboardFactory()
            self._keyboards = factory
        return factory

    def _command_keyboard(self) -> str:
        return self._keyboard_factory().command_keyboard()

    def _approval_keyboard(self, approval_id: int) -> str:
        return self._keyboard_factory().approval_keyboard(approval_id)

    def _slash_confirm_keyboard(self, confirm_id: str) -> str:
        return self._keyboard_factory().slash_confirm_keyboard(confirm_id)

    def _clarify_keyboard(self, choices: list, clarify_id: str) -> str:
        return self._keyboard_factory().clarify_keyboard(choices, clarify_id)

    def _provider_keyboard(self, providers: list, page: int = 0) -> str:
        return self._keyboard_factory().provider_keyboard(providers, page)

    def _model_keyboard(self, provider: dict[str, Any], provider_index: int, page: int = 0) -> str:
        return self._keyboard_factory().model_keyboard(provider, provider_index, page)

    def _chunk_text(self, text: str) -> list[str]:
        if len(text) <= self.max_message_length:
            return [text] if text else []
        chunks: list[str] = []
        remaining = text
        while remaining:
            chunk = remaining[: self.max_message_length]
            split_at = max(chunk.rfind("\n\n"), chunk.rfind("\n"), chunk.rfind(" "))
            if split_at > self.max_message_length * 0.4:
                chunk = chunk[:split_at]
            chunk = chunk.strip()
            if not chunk:
                chunk = remaining[: self.max_message_length]
            chunks.append(chunk)
            remaining = remaining[len(chunk) :].strip()
        return chunks

    def _chunk_random_id(self, peer_id: int, index: int, rendered: str) -> int:
        """A ``random_id`` that is stable across retries of one logical chunk.

        VK deduplicates ``messages.send`` by ``random_id``, so a retry -- ours
        after a timeout, or the gateway's after a ``retryable`` SendResult --
        must present the same value or it becomes a second visible message.
        The id is cached under a content key for a short window: long enough to
        cover any retry of *this* send, short enough that the user genuinely
        repeating a message later still gets a fresh id instead of silently
        colliding with the earlier one.
        """
        assert self.client is not None
        digest = hashlib.blake2b(rendered.encode("utf-8"), digest_size=16).hexdigest()
        key = (peer_id, index, digest)
        return self._outbound_random_ids.setdefault(key, self.client.new_random_id)

    @staticmethod
    def _failed_send(exc: BaseException, *, error: str | None = None) -> SendResult:
        """Build a failed SendResult with a machine-readable classification."""
        return SendResult(
            success=False,
            error=error if error is not None else redact_secrets(exc),
            retryable=is_retryable(exc),
            error_kind=classify_vk_error_kind(exc),
        )

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if not self.client:
            return SendResult(success=False, error="VK adapter is not connected", retryable=True)
        peer_id = _safe_int(chat_id)
        if not peer_id:
            return SendResult(success=False, error=f"Invalid VK peer_id: {chat_id!r}")
        self._approval_counter += 1
        approval_id = self._approval_counter
        self._approval_state[approval_id] = session_key
        preview = command[:1800] + "..." if len(command) > 1800 else command
        text = f"Command approval required\n\n{preview}\n\nReason: {description}"
        try:
            response = await self.client.send_message(
                peer_id=peer_id,
                message=self.format_message(text),
                keyboard=self._approval_keyboard(approval_id),
            )
            return SendResult(success=True, message_id=str(response))
        except Exception as exc:
            self._approval_state.pop(approval_id, None)
            return self._failed_send(exc)

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if not self.client:
            return SendResult(success=False, error="VK adapter is not connected", retryable=True)
        peer_id = _safe_int(chat_id)
        if not peer_id:
            return SendResult(success=False, error=f"Invalid VK peer_id: {chat_id!r}")
        self._slash_confirm_state[confirm_id] = session_key
        try:
            response = await self.client.send_message(
                peer_id=peer_id,
                message=self.format_message(message),
                keyboard=self._slash_confirm_keyboard(confirm_id),
            )
            return SendResult(success=True, message_id=str(response))
        except Exception as exc:
            self._slash_confirm_state.pop(confirm_id, None)
            return self._failed_send(exc)

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: list | None,
        clarify_id: str,
        session_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if not self.client:
            return SendResult(success=False, error="VK adapter is not connected", retryable=True)
        peer_id = _safe_int(chat_id)
        if not peer_id:
            return SendResult(success=False, error=f"Invalid VK peer_id: {chat_id!r}")
        clean_choices = [str(choice).strip() for choice in (choices or []) if str(choice).strip()]
        if clean_choices:
            lines = [f"Question: {question}", ""]
            lines.extend(
                f"{index}. {choice}"
                for index, choice in enumerate(clean_choices[:8], start=1)
            )
            keyboard = self._clarify_keyboard(clean_choices, clarify_id)
        else:
            lines = [f"Question: {question}", "", "Reply in this chat with your answer."]
            keyboard = None
            try:
                from tools.clarify_gateway import mark_awaiting_text

                mark_awaiting_text(clarify_id)
            except Exception:
                pass
        self._clarify_state[clarify_id] = session_key
        try:
            response = await self.client.send_message(
                peer_id=peer_id,
                message=self.format_message("\n".join(lines)),
                keyboard=keyboard,
            )
            return SendResult(success=True, message_id=str(response))
        except Exception as exc:
            self._clarify_state.pop(clarify_id, None)
            return self._failed_send(exc)

    async def send_model_picker(
        self,
        chat_id: str,
        providers: list,
        current_model: str,
        current_provider: str,
        session_key: str,
        on_model_selected,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if not self.client:
            return SendResult(success=False, error="VK adapter is not connected", retryable=True)
        peer_id = _safe_int(chat_id)
        if not peer_id:
            return SendResult(success=False, error=f"Invalid VK peer_id: {chat_id!r}")
        self._model_picker_state[str(peer_id)] = {
            "providers": providers,
            "session_key": session_key,
            "on_model_selected": on_model_selected,
            "current_model": current_model,
            "current_provider": current_provider,
            "provider_page": 0,
        }
        try:
            response = await self.client.send_message(
                peer_id=peer_id,
                message=model_picker_provider_text(providers, current_model, current_provider),
                keyboard=self._provider_keyboard(providers),
            )
            return SendResult(success=True, message_id=str(response))
        except Exception as exc:
            self._model_picker_state.pop(str(peer_id), None)
            return self._failed_send(exc)


async def _standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> dict[str, Any]:
    extra = getattr(pconfig, "extra", {}) or {}
    token = os.getenv("VK_GROUP_TOKEN") or getattr(pconfig, "token", "") or extra.get("token", "")
    group_id = _safe_int(os.getenv("VK_GROUP_ID") or extra.get("group_id"), 0)
    api_version = os.getenv("VK_API_VERSION") or extra.get("api_version") or DEFAULT_API_VERSION
    peer_id = _safe_int(chat_id, 0)
    if not token or not group_id or not peer_id:
        return {"error": "VK standalone send missing token/group_id/peer_id"}

    client = VKRestClient(token, group_id, api_version)
    try:
        attachment = None
        if media_files:
            refs = [
                await client.upload_document_raw(peer_id=peer_id, path=path)
                for path in media_files
            ]
            attachment = ",".join(refs)
        response = await client.send_message(
            peer_id=peer_id, message=message, attachment=attachment
        )
        return {"success": True, "message_id": str(response)}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        await client.close()


def check_requirements() -> bool:
    try:
        import httpx as _httpx  # noqa: F401
    except Exception:
        return False
    return bool(os.getenv("VK_GROUP_TOKEN") and os.getenv("VK_GROUP_ID"))


def validate_config(config: PlatformConfig) -> bool:
    extra = getattr(config, "extra", {}) or {}
    token = os.getenv("VK_GROUP_TOKEN") or getattr(config, "token", "") or extra.get("token", "")
    group_id = os.getenv("VK_GROUP_ID") or extra.get("group_id", "")
    return bool(token and str(group_id).strip())


def _env_enablement() -> dict[str, Any] | None:
    token = os.getenv("VK_GROUP_TOKEN", "").strip()
    group_id = os.getenv("VK_GROUP_ID", "").strip()
    if not token or not group_id:
        return None
    seed: dict[str, Any] = {
        "token": token,
        "group_id": group_id,
        "api_version": os.getenv("VK_API_VERSION", DEFAULT_API_VERSION),
        "require_mention": os.getenv("VK_REQUIRE_MENTION", "true"),
    }
    if home := os.getenv("VK_HOME_PEER_ID", "").strip():
        seed["home_channel"] = {"chat_id": home, "name": "VK Hermes"}
    return seed


def register(ctx: Any) -> None:
    ctx.register_platform(
        name="vk",
        label="VK",
        adapter_factory=lambda cfg: VKAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["VK_GROUP_TOKEN", "VK_GROUP_ID"],
        install_hint="pip install httpx",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="VK_HOME_PEER_ID",
        allowed_users_env="VK_ALLOWED_USERS",
        allow_all_env="VK_ALLOW_ALL_USERS",
        max_message_length=_safe_int(
            os.getenv("VK_MAX_MESSAGE_LENGTH"), DEFAULT_MAX_MESSAGE_LENGTH
        ),
        platform_hint=(
            "You are chatting through VK. Keep formatting simple and avoid "
            "Telegram-specific markdown."
        ),
        emoji="VK",
        standalone_sender_fn=_standalone_send,
    )
