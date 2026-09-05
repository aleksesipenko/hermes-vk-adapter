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
from plugins.vk.state import BoundedTTLCache as _BoundedTTLCache
from plugins.vk.utils import ReactionConfig as _ReactionConfig
from plugins.vk.utils import _vk_attachment_ref

# Importing the adapter requires the real Hermes contract.
pytestmark = pytest.mark.hermes_contract


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
async def test_longpoll_failed_two_renews_the_key_but_keeps_our_position():
    """failed=2 means only the key expired.

    Adopting VK's fresh ts here would silently skip every event that arrived
    between our last poll and the key renewal. The full-refresh behaviour
    belongs to failed=3; see tests/test_vk_longpoll.py for the whole matrix.
    """

    class FakeClient:
        async def get_long_poll_state(self):
            return LongPollState(server="new-server", key="new-key", ts="new-ts")

    adapter = object.__new__(VKAdapter)
    adapter.client = FakeClient()
    adapter.longpoll_state = LongPollState(server="old-server", key="old-key", ts="old-ts")

    assert await VKAdapter._handle_longpoll_failed(adapter, {"failed": 2}) is True

    assert adapter.longpoll_state == LongPollState("new-server", "new-key", "old-ts")


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

        def register_cli_command(self, **kwargs):
            pass

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

    media_paths, media_types, message_type, _notes = await VKAdapter._extract_media(
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

    media_paths, media_types, message_type, _notes = await VKAdapter._extract_media(
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

    media_paths, media_types, message_type, _notes = await VKAdapter._extract_media(
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

    media_paths, media_types, message_type, _notes = await VKAdapter._extract_media(
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
        return [], [], MessageType.TEXT, []

    adapter.handle_message = fake_handle_message
    adapter._extract_media = fake_extract_media
    adapter._last_actor = _BoundedTTLCache(max_entries=16, ttl_seconds=3600)
    adapter._last_inbound_cmid = _BoundedTTLCache(max_entries=16, ttl_seconds=3600)
    adapter._capabilities = _BoundedTTLCache(max_entries=16, ttl_seconds=600)
    adapter._identities = _BoundedTTLCache(max_entries=16, ttl_seconds=600)
    adapter.client = None  # identity lookups degrade to numeric labels
    adapter.reactions = _ReactionConfig()
    adapter.mark_read_enabled = False

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
        return [], [], MessageType.TEXT, []

    adapter.handle_message = fake_handle_message
    adapter._extract_media = fake_extract_media
    adapter._last_actor = _BoundedTTLCache(max_entries=16, ttl_seconds=3600)
    adapter._last_inbound_cmid = _BoundedTTLCache(max_entries=16, ttl_seconds=3600)
    adapter._capabilities = _BoundedTTLCache(max_entries=16, ttl_seconds=600)
    adapter._identities = _BoundedTTLCache(max_entries=16, ttl_seconds=600)
    adapter.client = None  # identity lookups degrade to numeric labels
    adapter.reactions = _ReactionConfig()
    adapter.mark_read_enabled = False

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

    provider_keyboard = json.loads(VKAdapter._provider_keyboard(adapter, providers, "pick1"))
    model_keyboard = json.loads(
        VKAdapter._model_keyboard(
            adapter,
            {
                "name": "Provider",
                "slug": "provider",
                "models": [f"model-{index}" for index in range(10)],
            },
            "pick1",
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

    provider_keyboard = json.loads(VKAdapter._provider_keyboard(adapter, providers, "pick1"))
    provider_buttons = [button for row in provider_keyboard["buttons"] for button in row]
    provider_labels = [button["action"]["label"] for button in provider_buttons]
    provider_payloads = [button["action"].get("payload") for button in provider_buttons]

    assert provider_labels[:6] == ["1", "2", "3", "4", "5", "6"]
    assert "Next" in provider_labels
    assert "Close" in provider_labels
    assert provider_labels.index("Close") < provider_labels.index("Next")
    assert all("Provider" not in label for label in provider_labels)
    assert {"h": "mpc", "i": "pick1", "pg": 0, "p": 0} in provider_payloads
    assert {"h": "mp", "i": "pick1", "pg": 1} in provider_payloads
    assert {"h": "mc", "i": "pick1"} in provider_payloads

    model_keyboard = json.loads(VKAdapter._model_keyboard(adapter, providers[0], "pick1", 0))
    model_buttons = [button for row in model_keyboard["buttons"] for button in row]
    model_labels = [button["action"]["label"] for button in model_buttons]
    model_payloads = [button["action"].get("payload") for button in model_buttons]

    assert model_labels[:6] == ["1", "2", "3", "4", "5", "6"]
    assert model_labels[6:] == ["Back", "Next", "Close"]
    assert "Close" in model_labels
    assert all("model" not in label.lower() for label in model_labels)
    assert {"h": "mb", "i": "pick1"} in model_payloads
    assert {"h": "mm", "i": "pick1", "p": 0, "pg": 0, "m": 0} in model_payloads


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










# ── outbound idempotency and typed failures (Task 3) ──────────────────────


def _idempotent_adapter():
    """An adapter wired just enough to exercise send()."""
    from plugins.vk.interactive import InteractiveStore
    from plugins.vk.state import BoundedTTLCache
    from plugins.vk.utils import ReactionConfig

    class FakeClient:
        def __init__(self):
            self.sends = []
            self._next = 0

        def new_random_id(self):
            self._next += 1
            return self._next

        async def send_message(self, **kwargs):
            self.sends.append(kwargs)
            return 100 + len(self.sends)

    adapter = object.__new__(VKAdapter)
    adapter.client = FakeClient()
    adapter.max_message_length = 9000
    adapter.command_keyboard_enabled = False
    adapter._outbound_random_ids = BoundedTTLCache(max_entries=64, ttl_seconds=120)
    adapter._interactive = InteractiveStore()
    adapter._last_actor = BoundedTTLCache(max_entries=16, ttl_seconds=3600)
    adapter._last_inbound_cmid = BoundedTTLCache(max_entries=16, ttl_seconds=3600)
    adapter._capabilities = BoundedTTLCache(max_entries=16, ttl_seconds=600)
    adapter._identities = BoundedTTLCache(max_entries=16, ttl_seconds=600)
    adapter.reactions = ReactionConfig()
    adapter.mark_read_enabled = False
    return adapter


@pytest.mark.asyncio
async def test_repeated_send_of_identical_content_reuses_one_random_id():
    """A gateway-level retry must not become a second visible VK message."""
    adapter = _idempotent_adapter()

    first = await VKAdapter.send(adapter, chat_id="987654321", content="same text")
    second = await VKAdapter.send(adapter, chat_id="987654321", content="same text")

    assert first.success and second.success
    random_ids = [call["random_id"] for call in adapter.client.sends]
    assert len(random_ids) == 2
    assert random_ids[0] == random_ids[1]
    assert random_ids[0] > 0


@pytest.mark.asyncio
async def test_distinct_content_and_distinct_peers_get_distinct_random_ids():
    adapter = _idempotent_adapter()

    await VKAdapter.send(adapter, chat_id="987654321", content="alpha")
    await VKAdapter.send(adapter, chat_id="987654321", content="beta")
    await VKAdapter.send(adapter, chat_id="123123123", content="alpha")

    random_ids = [call["random_id"] for call in adapter.client.sends]
    assert len(set(random_ids)) == 3


@pytest.mark.asyncio
async def test_each_chunk_of_one_message_gets_its_own_random_id():
    adapter = _idempotent_adapter()
    adapter.max_message_length = 10

    await VKAdapter.send(adapter, chat_id="987654321", content="hello world hello world")

    random_ids = [call["random_id"] for call in adapter.client.sends]
    assert len(random_ids) > 1
    assert len(set(random_ids)) == len(random_ids)


@pytest.mark.asyncio
async def test_reply_to_fallback_keeps_the_same_random_id():
    """VK rejected the request, so nothing was delivered under that id."""
    adapter = _idempotent_adapter()

    class RejectingClient(type(adapter.client)):
        async def send_message(self, **kwargs):
            self.sends.append(kwargs)
            if kwargs.get("reply_to"):
                raise VKApiError(
                    "messages.send",
                    {"error": {"error_code": 100, "error_msg": "invalid reply_to"}},
                )
            return 777

    adapter.client = RejectingClient()

    result = await VKAdapter.send(
        adapter, chat_id="987654321", content="text", reply_to="42"
    )

    assert result.success
    random_ids = [call["random_id"] for call in adapter.client.sends]
    assert len(random_ids) == 2
    assert random_ids[0] == random_ids[1]


@pytest.mark.asyncio
async def test_permanent_vk_failure_is_non_retryable_and_classified():
    adapter = _idempotent_adapter()

    class ForbiddenClient(type(adapter.client)):
        async def send_message(self, **kwargs):
            raise VKApiError(
                "messages.send",
                {"error": {"error_code": 901, "error_msg": "no permission"}},
            )

    adapter.client = ForbiddenClient()

    result = await VKAdapter.send(adapter, chat_id="987654321", content="text")

    assert not result.success
    assert result.retryable is False
    assert result.error_kind == "forbidden"


@pytest.mark.asyncio
async def test_transport_failure_is_retryable_and_classified_transient():
    import httpx

    adapter = _idempotent_adapter()

    class FlakyClient(type(adapter.client)):
        async def send_message(self, **kwargs):
            raise httpx.ConnectError("connection refused")

    adapter.client = FlakyClient()

    result = await VKAdapter.send(adapter, chat_id="987654321", content="text")

    assert not result.success
    assert result.retryable is True
    assert result.error_kind == "transient"


@pytest.mark.asyncio
async def test_send_failure_message_never_carries_a_token():
    adapter = _idempotent_adapter()
    token = "b" * 85

    class LeakyClient(type(adapter.client)):
        async def send_message(self, **kwargs):
            raise RuntimeError(f"boom access_token={token}")

    adapter.client = LeakyClient()

    result = await VKAdapter.send(adapter, chat_id="987654321", content="text")

    assert not result.success
    assert token not in (result.error or "")


@pytest.mark.asyncio
async def test_interactive_surfaces_report_typed_failures_and_drop_their_state():
    """Every interactive surface shares send()'s failure classification.

    These four paths each have their own ``except`` block, so a helper renamed
    in ``send()`` alone leaves them raising ``AttributeError`` from inside the
    handler that was supposed to report the failure -- the surface then dies
    with an unhandled exception instead of a clean, retryable SendResult.
    """
    import httpx

    adapter = _idempotent_adapter()

    class DownClient(type(adapter.client)):
        async def send_message(self, **kwargs):
            raise httpx.ConnectError("connection refused")

    from plugins.vk.keyboards import VKKeyboardFactory

    adapter.client = DownClient()
    adapter._keyboards = VKKeyboardFactory()
    adapter._approval_counter = 0

    results = [
        await VKAdapter.send_exec_approval(
            adapter, chat_id="987654321", command="rm -rf /", session_key="s"
        ),
        await VKAdapter.send_slash_confirm(
            adapter, chat_id="987654321", title="t", message="m", session_key="s", confirm_id="c"
        ),
        await VKAdapter.send_clarify(
            adapter,
            chat_id="987654321",
            question="q",
            choices=["a", "b"],
            clarify_id="cl",
            session_key="s",
        ),
        await VKAdapter.send_model_picker(
            adapter,
            chat_id="987654321",
            providers=[{"slug": "p", "name": "P", "models": ["m"]}],
            current_model="m",
            current_provider="p",
            session_key="s",
            on_model_selected=None,
        ),
    ]

    for result in results:
        assert not result.success
        assert result.retryable is True
        assert result.error_kind == "transient"

    # Pending state must not survive a send that never reached the user: the
    # button would authorize a prompt the user never saw.
    for kind, action_id in (("ea", "1"), ("sc", "c"), ("cl", "cl")):
        action, reason = adapter._interactive.authorize(
            kind=kind, action_id=action_id, user_id=987654321, peer_id=987654321
        )
        assert action is None, f"{kind} state survived a failed send"
        assert reason is not None


# ── reply/forward/attachment context reaches Hermes (Task 9) ──────────────


async def _capture_inbound(adapter, message):
    captured = []

    async def fake_handle_message(event):
        captured.append(event)

    adapter.handle_message = fake_handle_message
    adapter.build_source = lambda **kw: None
    adapter.group_id = 123456789
    adapter.require_mention = False
    adapter.allow_all_users = True
    adapter.allowed_users = set()
    adapter.mark_read_enabled = False
    adapter._last_actor = _BoundedTTLCache(max_entries=8, ttl_seconds=600)
    adapter._last_inbound_cmid = _BoundedTTLCache(max_entries=8, ttl_seconds=600)
    adapter._capabilities = _BoundedTTLCache(max_entries=8, ttl_seconds=600)
    adapter._identities = _BoundedTTLCache(max_entries=8, ttl_seconds=600)
    adapter.reactions = _ReactionConfig()
    await VKAdapter._handle_message_new(adapter, message, {"type": "message_new"})
    return captured


@pytest.mark.asyncio
async def test_a_reply_and_a_forward_reach_the_agent_as_text():
    adapter = object.__new__(VKAdapter)
    adapter.client = None

    async def no_media(_message):
        return [], [], MessageType.TEXT, []

    adapter._extract_media = no_media

    captured = await _capture_inbound(
        adapter,
        {
            "peer_id": 987654321,
            "from_id": 987654321,
            "id": 7,
            "text": "what do you think?",
            "reply_message": {"from_id": 5, "text": "the original proposal"},
            "fwd_messages": [{"from_id": 6, "text": "a forwarded remark"}],
        },
    )

    assert len(captured) == 1
    text = captured[0].text
    assert "what do you think?" in text
    assert "the original proposal" in text
    assert "a forwarded remark" in text


@pytest.mark.asyncio
async def test_an_unsupported_attachment_is_described_instead_of_dropped(monkeypatch):
    adapter = object.__new__(VKAdapter)
    adapter.client = None

    captured = await _capture_inbound(
        adapter,
        {
            "peer_id": 987654321,
            "from_id": 987654321,
            "id": 8,
            "text": "",
            "attachments": [
                {"type": "link", "link": {"url": "https://example.com", "title": "Example"}}
            ],
        },
    )

    assert len(captured) == 1
    assert "https://example.com" in captured[0].text


@pytest.mark.asyncio
async def test_one_failing_attachment_preserves_the_others(monkeypatch):
    adapter = object.__new__(VKAdapter)

    async def exploding_cache(url, ext=".jpg"):
        raise RuntimeError("download failed")

    monkeypatch.setattr("plugins.vk.adapter.cache_image_from_url", exploding_cache)

    media_paths, _types, _kind, notes = await VKAdapter._extract_media(
        adapter,
        {
            "attachments": [
                {"type": "photo", "photo": {"sizes": [{"width": 1, "height": 1, "url": "u"}]}},
                {"type": "poll", "poll": {"question": "Which one?"}},
            ]
        },
    )

    assert media_paths == []
    assert any("unavailable" in note for note in notes)
    assert any("Which one?" in note for note in notes)


@pytest.mark.asyncio
async def test_multiple_images_go_out_as_one_native_photo_batch(tmp_path):
    adapter = _idempotent_adapter()
    uploaded = []

    class PhotoClient(type(adapter.client)):
        async def upload_photo_message_raw(self, *, peer_id, path):
            uploaded.append(path)
            return f"photo-1_{len(uploaded)}"

    adapter.client = PhotoClient()
    paths = []
    for index in range(3):
        image = tmp_path / f"shot{index}.png"
        image.write_bytes(b"png")
        paths.append(str(image))

    result = await VKAdapter.send_media_auto(adapter, "987654321", paths, caption="three shots")

    assert result.success
    assert len(uploaded) == 3
    assert len(adapter.client.sends) == 1
    assert adapter.client.sends[0]["attachment"].count(",") == 2


@pytest.mark.asyncio
async def test_a_failed_photo_batch_still_delivers_via_documents(tmp_path):
    adapter = _idempotent_adapter()
    documents = []

    class FailingPhotos(type(adapter.client)):
        async def upload_photo_message_raw(self, *, peer_id, path):
            raise RuntimeError("photos scope missing")

        async def upload_document_raw(self, *, peer_id, path, title=None):
            documents.append(path)
            return f"doc-1_{len(documents)}"

    adapter.client = FailingPhotos()
    paths = []
    for index in range(2):
        image = tmp_path / f"shot{index}.png"
        image.write_bytes(b"png")
        paths.append(str(image))

    result = await VKAdapter.send_media_auto(adapter, "987654321", paths, caption="shots")

    assert result.success
    assert documents == paths
