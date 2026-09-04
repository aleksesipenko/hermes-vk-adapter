from __future__ import annotations

from pathlib import Path

import pytest
import respx
from httpx import Response

from plugins.vk.adapter import VKAdapter, VKApiError, VKRestClient


def _image_bytes(suffix: str) -> bytes:
    match suffix:
        case ".jpg" | ".jpeg":
            return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x01\x00\x01\x00\x00\xff\xd9"
        case ".png":
            return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 13
        case ".gif":
            return b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
        case unreachable:
            raise AssertionError(f"Unsupported test image suffix: {unreachable}")


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("suffix", "mime_type"),
    [
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".png", "image/png"),
        (".gif", "image/gif"),
    ],
)
async def test_raw_photo_upload_flow_for_vk_supported_formats(
    tmp_path: Path,
    suffix: str,
    mime_type: str,
) -> None:
    image_path = tmp_path / f"image{suffix}"
    image_path.write_bytes(_image_bytes(suffix))

    client = VKRestClient("token", group_id=123456789, api_version="5.199")
    get_server = respx.post("https://api.vk.com/method/photos.getMessagesUploadServer").mock(
        return_value=Response(200, json={"response": {"upload_url": "https://upload.example/photo"}})
    )
    upload = respx.post("https://upload.example/photo").mock(
        return_value=Response(200, json={"server": 231331, "photo": "[{}]", "hash": "hash-token"})
    )
    save = respx.post("https://api.vk.com/method/photos.saveMessagesPhoto").mock(
        return_value=Response(
            200,
            json={
                "response": [
                    {"owner_id": -123456789, "id": 457239023, "access_key": "photo-access-key"}
                ]
            },
        )
    )

    try:
        ref = await client.upload_photo_message_raw(peer_id=987654321, path=str(image_path))
    finally:
        await client.close()

    assert ref == "photo-123456789_457239023_photo-access-key"
    assert b"peer_id=987654321" in get_server.calls.last.request.content
    assert image_path.name.encode() in upload.calls.last.request.content
    assert mime_type.encode() in upload.calls.last.request.content
    assert b"server=231331" in save.calls.last.request.content
    assert b"photo=%5B%7B%7D%5D" in save.calls.last.request.content
    assert b"hash=hash-token" in save.calls.last.request.content


@pytest.mark.asyncio
@respx.mock
async def test_raw_photo_upload_rejects_empty_vk_photo_payload(tmp_path: Path) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(_image_bytes(".png"))

    client = VKRestClient("token", group_id=123456789, api_version="5.199")
    respx.post("https://api.vk.com/method/photos.getMessagesUploadServer").mock(
        return_value=Response(200, json={"response": {"upload_url": "https://upload.example/photo"}})
    )
    respx.post("https://upload.example/photo").mock(
        return_value=Response(200, json={"server": 231331, "photo": "", "hash": "hash-token"})
    )
    save = respx.post("https://api.vk.com/method/photos.saveMessagesPhoto").mock(
        return_value=Response(200, json={"response": []})
    )

    try:
        with pytest.raises(RuntimeError, match="did not return save parameters"):
            await client.upload_photo_message_raw(peer_id=987654321, path=str(image_path))
    finally:
        await client.close()

    assert not save.called


@pytest.mark.asyncio
async def test_send_image_file_uses_vk_native_photo_upload(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.photo_uploads: list[dict[str, str | int]] = []
            self.document_uploads: list[dict[str, str | int | None]] = []
            self.messages: list[dict[str, str | int]] = []

        async def upload_photo_message_raw(self, *, peer_id: int, path: str) -> str:
            self.photo_uploads.append({"peer_id": peer_id, "path": path})
            return "photo-123_456_access"

        async def upload_document_raw(
            self,
            *,
            peer_id: int,
            path: str,
            title: str | None = None,
        ) -> str:
            self.document_uploads.append({"peer_id": peer_id, "path": path, "title": title})
            return "doc-123_456"

        async def send_message(self, **kwargs: str | int) -> int:
            self.messages.append(kwargs)
            return 321

    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"png")

    adapter = object.__new__(VKAdapter)
    adapter.client = FakeClient()

    result = await VKAdapter.send_image_file(
        adapter,
        chat_id="987654321",
        image_path=str(image_path),
        caption="Preview",
        reply_to="55",
    )

    assert result.success
    assert result.message_id == "321"
    assert adapter.client.photo_uploads == [{"peer_id": 987654321, "path": str(image_path)}]
    assert adapter.client.document_uploads == []
    assert adapter.client.messages == [
        {
            "peer_id": 987654321,
            "message": "Preview",
            "attachment": "photo-123_456_access",
            "reply_to": "55",
        }
    ]


@pytest.mark.asyncio
async def test_send_image_file_falls_back_to_document_for_unsupported_webp(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.photo_uploads: list[dict[str, str | int]] = []
            self.document_uploads: list[dict[str, str | int | None]] = []
            self.messages: list[dict[str, str | int]] = []

        async def upload_photo_message_raw(self, *, peer_id: int, path: str) -> str:
            self.photo_uploads.append({"peer_id": peer_id, "path": path})
            return "photo-123_456"

        async def upload_document_raw(
            self,
            *,
            peer_id: int,
            path: str,
            title: str | None = None,
        ) -> str:
            self.document_uploads.append({"peer_id": peer_id, "path": path, "title": title})
            return "doc-123_456"

        async def send_message(self, **kwargs: str | int) -> int:
            self.messages.append(kwargs)
            return 654

    image_path = tmp_path / "image.webp"
    image_path.write_bytes(b"webp")

    adapter = object.__new__(VKAdapter)
    adapter.client = FakeClient()

    result = await VKAdapter.send_image_file(
        adapter, chat_id="987654321", image_path=str(image_path)
    )

    assert result.success
    assert result.message_id == "654"
    assert adapter.client.photo_uploads == []
    assert adapter.client.document_uploads == [
        {"peer_id": 987654321, "path": str(image_path), "title": "image.webp"}
    ]
    assert adapter.client.messages == [
        {"peer_id": 987654321, "message": "", "attachment": "doc-123_456"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "photo_error",
    [
        VKApiError(
            "photos.getMessagesUploadServer",
            {
                "error": {
                    "error_code": 15,
                    "error_msg": "Access denied: no access to call this method.",
                }
            },
        ),
        ValueError("Malformed VK photo upload response"),
    ],
)
async def test_send_image_file_falls_back_to_document_when_vk_photo_upload_fails(
    tmp_path: Path,
    photo_error: Exception,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.document_uploads: list[dict[str, str | int | None]] = []
            self.messages: list[dict[str, str | int]] = []

        async def upload_photo_message_raw(self, *, peer_id: int, path: str) -> str:
            raise photo_error

        async def upload_document_raw(
            self,
            *,
            peer_id: int,
            path: str,
            title: str | None = None,
        ) -> str:
            self.document_uploads.append({"peer_id": peer_id, "path": path, "title": title})
            return "doc-123_456"

        async def send_message(self, **kwargs: str | int) -> int:
            self.messages.append(kwargs)
            return 987

    image_path = tmp_path / "image.jpg"
    image_path.write_bytes(b"jpg")

    adapter = object.__new__(VKAdapter)
    adapter.client = FakeClient()

    result = await VKAdapter.send_image_file(
        adapter,
        chat_id="987654321",
        image_path=str(image_path),
        caption="Fallback",
    )

    assert result.success
    assert result.message_id == "987"
    assert adapter.client.document_uploads == [
        {"peer_id": 987654321, "path": str(image_path), "title": "image.jpg"}
    ]
    assert adapter.client.messages == [
        {"peer_id": 987654321, "message": "Fallback", "attachment": "doc-123_456"}
    ]
