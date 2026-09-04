from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import respx
from gateway.platforms.base import MessageType
from httpx import Response

from plugins.vk.adapter import (
    LongPollState,
    VKAdapter,
    VKApiError,
    VKRestClient,
    _csv_set,
    _largest_photo_url,
    _safe_int,
    _truthy,
    register,
)
from plugins.vk.utils import _vk_attachment_ref


def test_truthy_csv_and_safe_int_helpers():
    assert _truthy("true")
    assert _truthy("YES")
    assert not _truthy("false")
    assert _csv_set("1, 2,,3") == {"1", "2", "3"}
    assert _safe_int("42") == 42
    assert _safe_int("nope", default=7) == 7


def test_connect_accepts_gateway_reconnect_flag():
    import inspect

    signature = inspect.signature(VKAdapter.connect)
    assert "is_reconnect" in signature.parameters
    assert signature.parameters["is_reconnect"].default is False


def test_attachment_ref_includes_access_key_when_present():
    assert _vk_attachment_ref("doc", {"owner_id": -1, "id": 2}) == "doc-1_2"
    assert (
        _vk_attachment_ref("photo", {"owner_id": -1, "id": 2, "access_key": "abc"})
        == "photo-1_2_abc"
    )
    assert _vk_attachment_ref("doc", {"owner_id": -1}) is None


def test_largest_photo_url_chooses_highest_area():
    photo = {
        "sizes": [
            {"width": 200, "height": 200, "url": "medium"},
            {"width": 1000, "height": 1000, "url": "large"},
            {"width": 10, "height": 10, "url": "small"},
        ]
    }

    assert _largest_photo_url(photo) == "large"


def test_chunk_text_keeps_chunks_under_limit_and_readable():
    adapter = object.__new__(VKAdapter)
    adapter.max_message_length = 10

    chunks = VKAdapter._chunk_text(adapter, "hello world hello world")

    assert chunks == ["hello", "world", "hello", "world"]
    assert all(len(chunk) <= 10 for chunk in chunks)


def test_group_activation_requires_command_mention_or_hermes_word():
    adapter = object.__new__(VKAdapter)
    adapter.group_id = 123456789

    assert VKAdapter._is_group_activation(adapter, "/start")
    assert VKAdapter._is_group_activation(adapter, "Hermes ping")
    assert VKAdapter._is_group_activation(adapter, "[club123456789|Hermes] ping")
    assert VKAdapter._is_group_activation(adapter, "@club123456789 ping")
    assert not VKAdapter._is_group_activation(adapter, "ordinary chat noise")


def test_allowed_users_defaults_to_allow_when_gateway_policy_handles_auth():
    adapter = object.__new__(VKAdapter)
    adapter.allow_all_users = False
    adapter.allowed_users = set()

    assert VKAdapter._is_allowed_vk_user(adapter, 987654321)

    adapter.allowed_users = {"987654321"}
    assert VKAdapter._is_allowed_vk_user(adapter, 987654321)
    assert not VKAdapter._is_allowed_vk_user(adapter, 42)

    adapter.allow_all_users = True
    assert VKAdapter._is_allowed_vk_user(adapter, 42)


@pytest.mark.asyncio
async def test_longpoll_failed_one_only_updates_ts():
    class FakeClient:
        async def get_long_poll_state(self):  # pragma: no cover - should not be called
            raise AssertionError("should not refetch for failed=1")

    adapter = object.__new__(VKAdapter)
    adapter.client = FakeClient()
    adapter.longpoll_state = LongPollState(server="s", key="k", ts="old")

    await VKAdapter._handle_longpoll_failed(adapter, {"failed": 1, "ts": "new"})

    assert adapter.longpoll_state.ts == "new"


@pytest.mark.asyncio
async def test_longpoll_failed_two_refetches_state():
    class FakeClient:
        async def get_long_poll_state(self):
            return LongPollState(server="new-server", key="new-key", ts="new-ts")

    adapter = object.__new__(VKAdapter)
    adapter.client = FakeClient()
    adapter.longpoll_state = LongPollState(server="old-server", key="old-key", ts="old-ts")

    await VKAdapter._handle_longpoll_failed(adapter, {"failed": 2})

    assert adapter.longpoll_state == LongPollState("new-server", "new-key", "new-ts")


@pytest.mark.asyncio
@respx.mock
async def test_raw_document_upload_flow(tmp_path: Path):
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello", encoding="utf-8")

    client = VKRestClient("token", group_id=123456789, api_version="5.199")
    respx.post("https://api.vk.com/method/docs.getMessagesUploadServer").mock(
        return_value=Response(200, json={"response": {"upload_url": "https://upload.example/doc"}})
    )
    respx.post("https://upload.example/doc").mock(
        return_value=Response(200, json={"file": "uploaded-token"})
    )
    respx.post("https://api.vk.com/method/docs.save").mock(
        return_value=Response(200, json={"response": {"doc": {"owner_id": -1, "id": 99}}})
    )

    try:
        ref = await client.upload_document_raw(peer_id=987654321, path=str(file_path))
        assert ref == "doc-1_99"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_send_document_accepts_gateway_file_path_contract(tmp_path: Path):
    class FakeClient:
        def __init__(self):
            self.uploads = []
            self.messages = []

        async def upload_document_raw(self, *, peer_id: int, path: str, title: str | None = None):
            self.uploads.append({"peer_id": peer_id, "path": path, "title": title})
            return "doc-1_99"

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)
            return 123

    file_path = tmp_path / "report.txt"
    file_path.write_text("hello", encoding="utf-8")

    adapter = object.__new__(VKAdapter)
    adapter.client = FakeClient()

    result = await VKAdapter.send_document(
        adapter,
        chat_id="987654321",
        file_path=str(file_path),
        caption="Report",
        file_name="visible-report.txt",
        metadata={"thread_id": "ignored-by-vk"},
    )

    assert result.success
    assert result.message_id == "123"
    assert adapter.client.uploads == [
        {"peer_id": 987654321, "path": str(file_path), "title": "visible-report.txt"}
    ]
    assert adapter.client.messages == [
        {"peer_id": 987654321, "message": "Report", "attachment": "doc-1_99"}
    ]


@pytest.mark.asyncio
async def test_send_document_explains_missing_vk_docs_scope(tmp_path: Path):
    class FakeClient:
        async def upload_document_raw(self, *, peer_id: int, path: str, title: str | None = None):
            raise VKApiError(
                "docs.getMessagesUploadServer",
                {
                    "error": {
                        "error_code": 15,
                        "error_msg": "Access denied: no access to call this method.",
                    }
                },
            )

    file_path = tmp_path / "report.txt"
    file_path.write_text("hello", encoding="utf-8")

    adapter = object.__new__(VKAdapter)
    adapter.client = FakeClient()

    result = await VKAdapter.send_document(
        adapter,
        chat_id="987654321",
        file_path=str(file_path),
    )

    assert not result.success
    assert "VK_GROUP_TOKEN" in (result.error or "")
    assert "docs" in (result.error or "")


def test_register_provides_platform_hooks():
    class Ctx:
        kwargs = None

        def register_platform(self, **kwargs):
            self.kwargs = kwargs

    ctx = Ctx()
    register(ctx)

    assert ctx.kwargs["name"] == "vk"
    assert ctx.kwargs["label"] == "VK"
    assert ctx.kwargs["required_env"] == ["VK_GROUP_TOKEN", "VK_GROUP_ID"]
    assert ctx.kwargs["cron_deliver_env_var"] == "VK_HOME_PEER_ID"
    assert callable(ctx.kwargs["adapter_factory"])
    assert callable(ctx.kwargs["env_enablement_fn"])
    assert callable(ctx.kwargs["standalone_sender_fn"])


def test_format_message_renders_vk_plain_text():
    adapter = object.__new__(VKAdapter)

    rendered = VKAdapter.format_message(
        adapter,
        "**Bold** and <b>HTML</b> with `code` and [docs](https://example.com).\n"
        "```python\nprint('ok')\n```",
    )

    assert "**" not in rendered
    assert "<b>" not in rendered
    assert "</b>" not in rendered
    assert "Bold" in rendered
    assert "HTML" in rendered
    assert "code" in rendered
    assert "docs: https://example.com" in rendered
    assert "print('ok')" in rendered


@pytest.mark.asyncio
async def test_extract_audio_message_routes_voice_for_gateway_stt(monkeypatch):
    adapter = object.__new__(VKAdapter)

    async def fake_cache_audio_from_url(url: str, ext: str = ".ogg"):
        assert url == "https://vk.example/voice.ogg"
        assert ext == ".ogg"
        return "/tmp/vk-voice.ogg"

    monkeypatch.setattr("plugins.vk.adapter.cache_audio_from_url", fake_cache_audio_from_url)

    media_paths, media_types, message_type = await VKAdapter._extract_media(
        adapter,
        {"attachments": [{"type": "audio_message", "audio_message": {"link_ogg": "https://vk.example/voice.ogg"}}]},
    )

    assert media_paths == ["/tmp/vk-voice.ogg"]
    assert media_types == ["audio/ogg"]
    assert message_type == MessageType.VOICE


@pytest.mark.asyncio
async def test_extract_photo_routes_image_for_gateway_vision(monkeypatch):
    adapter = object.__new__(VKAdapter)

    async def fake_cache_image_from_url(url: str, ext: str = ".jpg"):
        assert url == "https://vk.example/photo-large.jpg"
        assert ext == ".jpg"
        return "/tmp/vk-photo.jpg"

    monkeypatch.setattr("plugins.vk.adapter.cache_image_from_url", fake_cache_image_from_url)

    media_paths, media_types, message_type = await VKAdapter._extract_media(
        adapter,
        {
            "attachments": [
                {
                    "type": "photo",
                    "photo": {
                        "sizes": [
                            {"width": 100, "height": 100, "url": "https://vk.example/photo-small.jpg"},
                            {"width": 1000, "height": 1000, "url": "https://vk.example/photo-large.jpg"},
                        ]
                    },
                }
            ]
        },
    )

    assert media_paths == ["/tmp/vk-photo.jpg"]
    assert media_types == ["image/jpeg"]
    assert message_type == MessageType.PHOTO


@pytest.mark.asyncio
async def test_extract_image_document_routes_as_photo_for_gateway_vision(monkeypatch):
    adapter = object.__new__(VKAdapter)

    class FakeClient:
        async def download_bytes(self, url: str):
            assert url == "https://vk.example/screenshot.png"
            return b"\x89PNG\r\n\x1a\nfake"

    adapter.client = FakeClient()

    def fake_cache_image_from_bytes(data: bytes, ext: str = ".jpg"):
        assert data.startswith(b"\x89PNG")
        assert ext == ".png"
        return "/tmp/vk-screenshot.png"

    monkeypatch.setattr("plugins.vk.adapter.cache_image_from_bytes", fake_cache_image_from_bytes)

    media_paths, media_types, message_type = await VKAdapter._extract_media(
        adapter,
        {
            "attachments": [
                {
                    "type": "doc",
                    "doc": {
                        "url": "https://vk.example/screenshot.png",
                        "title": "screenshot.png",
                        "ext": "png",
                    },
                }
            ]
        },
    )

    assert media_paths == ["/tmp/vk-screenshot.png"]
    assert media_types == ["image/png"]
    assert message_type == MessageType.PHOTO


@pytest.mark.asyncio
async def test_extract_regular_document_uses_gateway_mime_type(monkeypatch):
    adapter = object.__new__(VKAdapter)

    class FakeClient:
        async def download_bytes(self, url: str):
            assert url == "https://vk.example/report.pdf"
            return b"%PDF-1.7"

    adapter.client = FakeClient()

    def fake_cache_document_from_bytes(data: bytes, filename: str):
        assert data.startswith(b"%PDF")
        assert filename == "report.pdf"
        return "/tmp/report.pdf"

    monkeypatch.setattr(
        "plugins.vk.adapter.cache_document_from_bytes", fake_cache_document_from_bytes
    )

    media_paths, media_types, message_type = await VKAdapter._extract_media(
        adapter,
        {
            "attachments": [
                {
                    "type": "doc",
                    "doc": {
                        "url": "https://vk.example/report.pdf",
                        "title": "report.pdf",
                        "ext": "pdf",
                    },
                }
            ]
        },
    )

    assert media_paths == ["/tmp/report.pdf"]
    assert media_types == ["application/pdf"]
    assert message_type == MessageType.DOCUMENT


@pytest.mark.asyncio
async def test_group_slash_command_bypasses_mention_and_uses_command_type():
    adapter = object.__new__(VKAdapter)
    adapter.group_id = 123456789
    adapter.require_mention = True
    adapter.allow_all_users = True
    adapter.allowed_users = set()
    adapter.platform = SimpleNamespace(value="vk")

    captured = []

    async def fake_handle_message(event):
        captured.append(event)

    async def fake_extract_media(message):
        return [], [], MessageType.TEXT

    adapter.handle_message = fake_handle_message
    adapter._extract_media = fake_extract_media

    await VKAdapter._handle_message_new(
        adapter,
        {
            "peer_id": 2_000_000_001,
            "from_id": 987654321,
            "text": "/status",
            "conversation_message_id": 7,
        },
        {"type": "message_new"},
    )

    assert len(captured) == 1
    assert captured[0].message_type == MessageType.COMMAND
    assert captured[0].text == "/status"


@pytest.mark.asyncio
async def test_group_reply_to_bot_activates_plain_text_message():
    adapter = object.__new__(VKAdapter)
    adapter.group_id = 123456789
    adapter.require_mention = True
    adapter.allow_all_users = True
    adapter.allowed_users = set()
    adapter.platform = SimpleNamespace(value="vk")

    captured = []

    async def fake_handle_message(event):
        captured.append(event)

    async def fake_extract_media(message):
        return [], [], MessageType.TEXT

    adapter.handle_message = fake_handle_message
    adapter._extract_media = fake_extract_media

    await VKAdapter._handle_message_new(
        adapter,
        {
            "peer_id": 2_000_000_001,
            "from_id": 987654321,
            "text": "continue",
            "conversation_message_id": 8,
            "reply_message": {"id": 3, "from_id": -123456789, "text": "Hermes response"},
        },
        {"type": "message_new"},
    )

    assert len(captured) == 1
    assert captured[0].message_type == MessageType.TEXT
    assert captured[0].reply_to_message_id == "3"


def test_vk_command_keyboard_is_valid_json_and_compact():
    adapter = object.__new__(VKAdapter)

    keyboard = VKAdapter._command_keyboard(adapter)
    data = json.loads(keyboard)

    labels = [button["action"]["label"] for row in data["buttons"] for button in row]
    assert data["inline"] is False
    assert "/commands" in labels
    assert "/status" in labels
    assert len(keyboard) < 1200


def test_vk_inline_keyboards_stay_within_row_limits():
    adapter = object.__new__(VKAdapter)
    providers = [
        {"name": f"Provider {index}", "slug": f"provider-{index}", "models": [f"model-{index}"]}
        for index in range(10)
    ]

    provider_keyboard = json.loads(VKAdapter._provider_keyboard(adapter, providers))
    model_keyboard = json.loads(
        VKAdapter._model_keyboard(
            adapter,
            {
                "name": "Provider",
                "slug": "provider",
                "models": [f"model-{index}" for index in range(10)],
            },
            0,
        )
    )
    clarify_keyboard = json.loads(
        VKAdapter._clarify_keyboard(adapter, [f"choice-{index}" for index in range(10)], "clarify")
    )

    assert len(provider_keyboard["buttons"]) <= 5
    assert len(model_keyboard["buttons"]) <= 5
    assert len(clarify_keyboard["buttons"]) <= 5
    assert all(len(row) <= 3 for row in provider_keyboard["buttons"])
    assert all(len(row) <= 3 for row in model_keyboard["buttons"])
    assert all(len(row) <= 2 for row in clarify_keyboard["buttons"])


def test_model_picker_uses_numbered_buttons_and_navigation():
    adapter = object.__new__(VKAdapter)
    providers = [
        {
            "name": f"Provider {index}",
            "slug": f"provider-{index}",
            "models": [f"vendor/model-{model_index}" for model_index in range(9)],
        }
        for index in range(9)
    ]

    provider_keyboard = json.loads(VKAdapter._provider_keyboard(adapter, providers))
    provider_buttons = [button for row in provider_keyboard["buttons"] for button in row]
    provider_labels = [button["action"]["label"] for button in provider_buttons]
    provider_payloads = [button["action"].get("payload") for button in provider_buttons]

    assert provider_labels[:6] == ["1", "2", "3", "4", "5", "6"]
    assert "Next" in provider_labels
    assert "Close" in provider_labels
    assert provider_labels.index("Close") < provider_labels.index("Next")
    assert all("Provider" not in label for label in provider_labels)
    assert {"h": "mpc", "pg": 0, "p": 0} in provider_payloads
    assert {"h": "mp", "pg": 1} in provider_payloads
    assert {"h": "mc"} in provider_payloads

    model_keyboard = json.loads(VKAdapter._model_keyboard(adapter, providers[0], 0))
    model_buttons = [button for row in model_keyboard["buttons"] for button in row]
    model_labels = [button["action"]["label"] for button in model_buttons]
    model_payloads = [button["action"].get("payload") for button in model_buttons]

    assert model_labels[:6] == ["1", "2", "3", "4", "5", "6"]
    assert model_labels[6:] == ["Back", "Next", "Close"]
    assert "Close" in model_labels
    assert all("model" not in label.lower() for label in model_labels)
    assert {"h": "mb"} in model_payloads
    assert {"h": "mm", "p": 0, "pg": 0, "m": 0} in model_payloads


@pytest.mark.asyncio
@respx.mock
async def test_raw_edit_and_message_event_answer_calls():
    client = VKRestClient("token", group_id=123456789, api_version="5.199")
    edit = respx.post("https://api.vk.com/method/messages.edit").mock(
        return_value=Response(200, json={"response": 1})
    )
    answer = respx.post("https://api.vk.com/method/messages.sendMessageEventAnswer").mock(
        return_value=Response(200, json={"response": 1})
    )

    try:
        assert await client.edit_message(peer_id=987654321, message_id=10, message="Updated")
        assert await client.send_message_event_answer(
            event_id="evt",
            user_id=987654321,
            peer_id=987654321,
            text="Done",
        )
    finally:
        await client.close()

    assert b"message_id=10" in edit.calls.last.request.content
    assert b"message=Updated" in edit.calls.last.request.content
    assert b"event_id=evt" in answer.calls.last.request.content
    assert b"show_snackbar" in answer.calls.last.request.content


@pytest.mark.asyncio
async def test_message_event_approval_resolves_once_and_denies_unauthorized(monkeypatch):
    calls = []

    def fake_resolve(session_key, choice):
        calls.append((session_key, choice))
        return 1

    monkeypatch.setattr("tools.approval.resolve_gateway_approval", fake_resolve)

    class FakeClient:
        def __init__(self):
            self.answers = []
            self.edits = []

        async def send_message_event_answer(self, **kwargs):
            self.answers.append(kwargs)
            return 1

        async def edit_message(self, **kwargs):
            self.edits.append(kwargs)
            return 1

    adapter = object.__new__(VKAdapter)
    adapter.client = FakeClient()
    adapter.allowed_users = {"987654321"}
    adapter.allow_all_users = False
    adapter._approval_state = {1: "vk:987654321"}

    await VKAdapter._handle_message_event(
        adapter,
        {
            "event_id": "bad",
            "user_id": 42,
            "peer_id": 987654321,
            "conversation_message_id": 11,
            "payload": {"h": "ea", "id": 1, "c": "once"},
        },
    )
    assert calls == []
    assert adapter._approval_state == {1: "vk:987654321"}
    assert "not authorized" in adapter.client.answers[-1]["text"].lower()

    await VKAdapter._handle_message_event(
        adapter,
        {
            "event_id": "ok",
            "user_id": 987654321,
            "peer_id": 987654321,
            "conversation_message_id": 11,
            "payload": {"h": "ea", "id": 1, "c": "once"},
        },
    )
    assert calls == [("vk:987654321", "once")]
    assert adapter._approval_state == {}


@pytest.mark.asyncio
async def test_clarify_callbacks_resolve_choice_and_other(monkeypatch):
    resolved = []
    awaiting = []

    fake_entry = SimpleNamespace(choices=["alpha", "beta"])

    monkeypatch.setattr("tools.clarify_gateway._entries", {"clarify-1": fake_entry}, raising=False)
    monkeypatch.setattr(
        "tools.clarify_gateway.resolve_gateway_clarify",
        lambda clarify_id, response: resolved.append((clarify_id, response)) or True,
    )
    monkeypatch.setattr(
        "tools.clarify_gateway.mark_awaiting_text",
        lambda clarify_id: awaiting.append(clarify_id),
    )

    class FakeClient:
        async def send_message_event_answer(self, **kwargs):
            return 1

        async def edit_message(self, **kwargs):
            return 1

    adapter = object.__new__(VKAdapter)
    adapter.client = FakeClient()
    adapter.allow_all_users = True
    adapter.allowed_users = set()
    adapter._clarify_state = {"clarify-1": "vk:987654321", "clarify-2": "vk:987654321"}

    await VKAdapter._handle_message_event(
        adapter,
        {
            "event_id": "choice",
            "user_id": 987654321,
            "peer_id": 987654321,
            "conversation_message_id": 12,
            "payload": {"h": "cl", "id": "clarify-1", "c": 1},
        },
    )
    assert resolved == [("clarify-1", "beta")]
    assert "clarify-1" not in adapter._clarify_state

    await VKAdapter._handle_message_event(
        adapter,
        {
            "event_id": "other",
            "user_id": 987654321,
            "peer_id": 987654321,
            "conversation_message_id": 13,
            "payload": {"h": "cl", "id": "clarify-2", "c": "other"},
        },
    )
    assert awaiting == ["clarify-2"]
    assert "clarify-2" in adapter._clarify_state


@pytest.mark.asyncio
async def test_model_picker_callbacks_call_selected_model():
    selected = []

    async def on_model_selected(chat_id, model_id, provider_slug):
        selected.append((chat_id, model_id, provider_slug))
        return f"Switched to {model_id}"

    class FakeClient:
        async def send_message_event_answer(self, **kwargs):
            return 1

        async def edit_message(self, **kwargs):
            return 1

    adapter = object.__new__(VKAdapter)
    adapter.client = FakeClient()
    adapter.allow_all_users = True
    adapter.allowed_users = set()
    adapter._model_picker_state = {
        "987654321": {
            "providers": [
                {
                    "slug": "openrouter",
                    "name": "OpenRouter",
                    "models": ["deepseek/chat", "qwen/chat"],
                }
            ],
            "on_model_selected": on_model_selected,
        }
    }

    await VKAdapter._handle_message_event(
        adapter,
        {
            "event_id": "model",
            "user_id": 987654321,
            "peer_id": 987654321,
            "conversation_message_id": 14,
            "payload": {"h": "mm", "p": 0, "pg": 0, "m": 1},
        },
    )

    assert selected == [("987654321", "qwen/chat", "openrouter")]


@pytest.mark.asyncio
async def test_model_picker_back_and_close_edit_picker_message():
    class FakeClient:
        def __init__(self):
            self.edits = []
            self.answers = []

        async def send_message_event_answer(self, **kwargs):
            self.answers.append(kwargs)
            return 1

        async def edit_message(self, **kwargs):
            self.edits.append(kwargs)
            return 1

    providers = [
        {"slug": f"provider-{index}", "name": f"Provider {index}", "models": [f"model-{index}"]}
        for index in range(9)
    ]
    adapter = object.__new__(VKAdapter)
    adapter.client = FakeClient()
    adapter.allow_all_users = True
    adapter.allowed_users = set()
    adapter._model_picker_state = {
        "987654321": {
            "providers": providers,
            "current_model": "model-0",
            "current_provider": "provider-0",
            "provider_page": 1,
        }
    }

    await VKAdapter._handle_message_event(
        adapter,
        {
            "event_id": "back",
            "user_id": 987654321,
            "peer_id": 987654321,
            "conversation_message_id": 14,
            "payload": {"h": "mb"},
        },
    )
    assert "Choose provider" in adapter.client.edits[-1]["message"]
    assert adapter._model_picker_state["987654321"]["provider_page"] == 1
    assert adapter.client.answers == []

    await VKAdapter._handle_message_event(
        adapter,
        {
            "event_id": "close",
            "user_id": 987654321,
            "peer_id": 987654321,
            "conversation_message_id": 14,
            "payload": {"h": "mc"},
        },
    )
    assert adapter.client.edits[-1]["keyboard"] is None
    assert "closed" in adapter.client.edits[-1]["message"].lower()
    assert "987654321" not in adapter._model_picker_state
    assert adapter.client.answers == []
