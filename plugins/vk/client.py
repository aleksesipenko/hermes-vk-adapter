"""Narrow raw VK API client for the Hermes VK adapter."""

from __future__ import annotations

import json
import logging
import mimetypes
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .utils import DEFAULT_API_VERSION, VK_API_BASE, _vk_attachment_ref

logger = logging.getLogger(__name__)


class VKApiError(RuntimeError):
    def __init__(self, method: str, payload: dict[str, Any]):
        error = payload.get("error") or payload
        self.method = method
        self.payload = payload
        if isinstance(error, dict):
            code = error.get("error_code", "unknown")
            message = error.get("error_msg", str(error))
        else:
            code = "unknown"
            message = str(error)
        super().__init__(f"VK API error in {method}: {code} {message}")


@dataclass
class LongPollState:
    server: str
    key: str
    ts: str


class VKRestClient:
    def __init__(self, token: str, group_id: int, api_version: str) -> None:
        self.token = token
        self.group_id = group_id
        self.api_version = api_version or DEFAULT_API_VERSION
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0))

    async def close(self) -> None:
        await self.http.aclose()

    async def call(self, method: str, **params: Any) -> Any:
        response = await self.http.post(
            f"{VK_API_BASE}/{method}",
            data={"access_token": self.token, "v": self.api_version, **params},
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise VKApiError(method, payload)
        return payload.get("response")

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
    ) -> Any:
        params: dict[str, Any] = {
            "peer_id": peer_id,
            "random_id": random.randint(1, 2_147_483_647),
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
        return await self.call("messages.send", **params)

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
            await self.call("messages.setActivity", peer_id=peer_id, type="typing", group_id=self.group_id)
        except Exception as exc:
            logger.debug("VK typing indicator failed for peer_id=%s: %s", peer_id, exc)

    async def download_bytes(self, url: str, *, max_bytes: int = 80 * 1024 * 1024) -> bytes:
        async with self.http.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"VK attachment exceeds max_bytes={max_bytes}")
                chunks.append(chunk)
            return b"".join(chunks)

    async def upload_document_raw(self, *, peer_id: int, path: str) -> str:
        upload_server = await self.call("docs.getMessagesUploadServer", type="doc", peer_id=peer_id)
        file_path = Path(path)
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        with file_path.open("rb") as file_handle:
            upload_response = await self.http.post(
                upload_server["upload_url"],
                files={"file": (file_path.name, file_handle, mime_type)},
            )
        upload_response.raise_for_status()
        uploaded = upload_response.json()
        file_token = uploaded.get("file")
        if not file_token:
            raise RuntimeError(f"VK upload did not return file token: {uploaded}")

        saved = await self.call("docs.save", file=file_token, title=file_path.name)
        doc = saved.get("doc") if isinstance(saved, dict) else None
        if not isinstance(doc, dict):
            raise RuntimeError(f"Unexpected docs.save response shape: {saved}")
        ref = _vk_attachment_ref("doc", doc)
        if not ref:
            raise RuntimeError(f"Cannot build VK doc attachment ref from: {doc}")
        return ref
