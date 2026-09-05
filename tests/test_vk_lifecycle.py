"""Edit, delete, read status and opt-in reactions."""

from __future__ import annotations

import pytest

from plugins.vk.adapter import VKAdapter
from plugins.vk.interactive import InteractiveStore
from plugins.vk.state import BoundedTTLCache
from plugins.vk.utils import VK_MESSAGE_EDIT_LIMIT, ReactionConfig

pytestmark = pytest.mark.hermes_contract

PEER = 987654321
GROUP_PEER = 2_000_000_001


class FakeClient:
    def __init__(self, *, edit_error=None, delete_error=None):
        self.edits = []
        self.sends = []
        self.deletes = []
        self.reads = []
        self.reactions = []
        self.reaction_deletes = []
        self._edit_error = edit_error
        self._delete_error = delete_error
        self._next = 0

    def new_random_id(self):
        self._next += 1
        return self._next

    async def edit_message(self, **kwargs):
        if self._edit_error:
            raise self._edit_error
        self.edits.append(kwargs)
        return 1

    async def send_message(self, **kwargs):
        self.sends.append(kwargs)
        return 900 + len(self.sends)

    async def delete_message(self, **kwargs):
        if self._delete_error:
            raise self._delete_error
        self.deletes.append(kwargs)
        return {str(kwargs.get("message_ids") or kwargs.get("cmids")): 1}

    async def mark_as_read(self, **kwargs):
        self.reads.append(kwargs)
        return 1

    async def send_reaction(self, **kwargs):
        self.reactions.append(kwargs)
        return 1

    async def delete_reaction(self, **kwargs):
        self.reaction_deletes.append(kwargs)
        return 1


def make_adapter(client=None, **overrides):
    adapter = object.__new__(VKAdapter)
    adapter.client = client or FakeClient()
    adapter.group_id = 123456789
    adapter.max_message_length = 9000
    adapter.command_keyboard_enabled = False
    adapter.allow_all_users = True
    adapter.allowed_users = set()
    adapter.require_mention = False
    adapter.reactions = ReactionConfig()
    adapter.mark_read_enabled = True
    adapter._outbound_random_ids = BoundedTTLCache(max_entries=32, ttl_seconds=120)
    adapter._interactive = InteractiveStore()
    adapter._last_actor = BoundedTTLCache(max_entries=32, ttl_seconds=3600)
    adapter._cmid_by_anchor = BoundedTTLCache(max_entries=32, ttl_seconds=3600)
    adapter._capabilities = BoundedTTLCache(max_entries=16, ttl_seconds=600)
    adapter._identities = BoundedTTLCache(max_entries=16, ttl_seconds=600)
    for key, value in overrides.items():
        setattr(adapter, key, value)
    return adapter


# ── edit ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_updates_the_message_in_place():
    adapter = make_adapter()

    result = await VKAdapter.edit_message(adapter, str(PEER), "555", "updated text")

    assert result.success
    assert result.message_id == "555"
    assert adapter.client.edits == [
        {"peer_id": PEER, "message_id": 555, "message": "updated text"}
    ]
    assert adapter.client.sends == []


@pytest.mark.asyncio
async def test_edit_renders_hermes_formatting_to_vk_plain_text():
    adapter = make_adapter()

    await VKAdapter.edit_message(adapter, str(PEER), "555", "**bold** and `code`")

    assert adapter.client.edits[0]["message"] == "bold and code"


@pytest.mark.asyncio
async def test_edit_rejects_an_unusable_message_id():
    adapter = make_adapter()

    result = await VKAdapter.edit_message(adapter, str(PEER), "not-a-number", "x")

    assert not result.success
    assert adapter.client.edits == []


@pytest.mark.asyncio
async def test_edit_overflow_keeps_every_character_via_continuations():
    """messages.edit caps at 4096; the tail must still reach the user."""
    adapter = make_adapter()
    content = "word " * 3000

    result = await VKAdapter.edit_message(adapter, str(PEER), "555", content, finalize=True)

    assert result.success
    edited = adapter.client.edits[0]["message"]
    assert len(edited) <= VK_MESSAGE_EDIT_LIMIT
    sent = [call["message"] for call in adapter.client.sends]
    assert sent, "the overflow tail was never sent"

    rebuilt = "".join([edited, *sent]).replace(" ", "")
    assert rebuilt == content.replace(" ", "")


@pytest.mark.asyncio
async def test_edit_overflow_reports_hermes_continuation_ordering():
    adapter = make_adapter()

    result = await VKAdapter.edit_message(adapter, str(PEER), "555", "word " * 3000)

    ids = ["555", *[str(900 + n) for n in range(1, len(adapter.client.sends) + 1)]]
    assert result.message_id == ids[-1]
    assert list(result.continuation_message_ids) == ids[:-1]


@pytest.mark.asyncio
async def test_edit_failure_is_typed_and_not_silently_swallowed():
    import httpx

    adapter = make_adapter(FakeClient(edit_error=httpx.ConnectError("down")))

    result = await VKAdapter.edit_message(adapter, str(PEER), "555", "x")

    assert not result.success
    assert result.retryable is True
    assert result.error_kind == "transient"


# ── delete ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_returns_true_and_targets_the_right_peer():
    adapter = make_adapter()

    assert await VKAdapter.delete_message(adapter, str(PEER), "555") is True
    assert adapter.client.deletes == [{"peer_id": PEER, "message_ids": 555}]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "abc", "0", None])
async def test_delete_refuses_unusable_ids_without_calling_vk(bad):
    adapter = make_adapter()

    assert await VKAdapter.delete_message(adapter, str(PEER), bad) is False
    assert adapter.client.deletes == []


@pytest.mark.asyncio
async def test_delete_failure_returns_false_rather_than_raising():
    adapter = make_adapter(FakeClient(delete_error=RuntimeError("nope")))

    assert await VKAdapter.delete_message(adapter, str(PEER), "555") is False


# ── reaction configuration ────────────────────────────────────────────────


def test_reactions_are_disabled_by_default():
    config = ReactionConfig.from_env({})

    assert config.enabled is False
    assert config.processing is None
    assert config.done is None


def test_reactions_accept_only_positive_numeric_ids():
    config = ReactionConfig.from_env(
        {"VK_REACTION_PROCESSING_ID": "1", "VK_REACTION_DONE_ID": "16"}
    )

    assert config.enabled is True
    assert config.processing == 1
    assert config.done == 16


@pytest.mark.parametrize(
    "raw",
    [
        {"VK_REACTION_PROCESSING_ID": "heart"},
        {"VK_REACTION_PROCESSING_ID": "-3"},
        {"VK_REACTION_PROCESSING_ID": "0"},
        {"VK_REACTION_PROCESSING_ID": ""},
        {"VK_REACTION_PROCESSING_ID": "1.5"},
    ],
)
def test_invalid_reaction_ids_are_ignored_not_guessed(raw):
    """No emoji-to-id table is invented; a bad value simply disables it."""
    config = ReactionConfig.from_env(raw)

    assert config.processing is None
    assert config.enabled is False


# ── reaction lifecycle ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_reactions_are_sent_when_unconfigured():
    adapter = make_adapter()

    await VKAdapter._react_processing(adapter, GROUP_PEER, 42)
    await VKAdapter._react_done(adapter, GROUP_PEER)

    assert adapter.client.reactions == []
    assert adapter.client.reaction_deletes == []


@pytest.mark.asyncio
async def test_configured_reactions_use_the_conversation_message_id():
    """VK reactions key on cmid, never on the global message id."""
    adapter = make_adapter(reactions=ReactionConfig(processing=1, done=16))

    await VKAdapter._react_processing(adapter, GROUP_PEER, 42)

    assert adapter.client.reactions == [
        {"peer_id": GROUP_PEER, "cmid": 42, "reaction_id": 1}
    ]


@pytest.mark.asyncio
async def test_done_reaction_replaces_the_processing_one():
    adapter = make_adapter(reactions=ReactionConfig(processing=1, done=16))
    adapter._cmid_by_anchor.set((GROUP_PEER, "7"), 42)

    await VKAdapter._react_done(adapter, GROUP_PEER, "7")

    assert adapter.client.reactions[-1] == {
        "peer_id": GROUP_PEER,
        "cmid": 42,
        "reaction_id": 16,
    }


@pytest.mark.asyncio
async def test_done_removes_the_reaction_when_no_done_id_is_configured():
    adapter = make_adapter(reactions=ReactionConfig(processing=1))
    adapter._cmid_by_anchor.set((GROUP_PEER, "7"), 42)

    await VKAdapter._react_done(adapter, GROUP_PEER, "7")

    assert adapter.client.reaction_deletes == [{"peer_id": GROUP_PEER, "cmid": 42}]


@pytest.mark.asyncio
async def test_reaction_failures_are_cosmetic_and_never_raise():
    class Failing(FakeClient):
        async def send_reaction(self, **kwargs):
            raise RuntimeError("reactions unavailable for this token")

    adapter = make_adapter(Failing(), reactions=ReactionConfig(processing=1))

    await VKAdapter._react_processing(adapter, GROUP_PEER, 42)  # must not raise


# ── inbound acknowledgement ───────────────────────────────────────────────


async def run_inbound(adapter, message, *, allowed=True):
    handled = []

    async def fake_handle_message(event):
        handled.append(event)

    async def fake_extract_media(_message):
        from gateway.platforms.base import MessageType

        return [], [], MessageType.TEXT, []

    adapter.handle_message = fake_handle_message
    adapter._extract_media = fake_extract_media
    adapter.allow_all_users = allowed
    adapter.allowed_users = set() if allowed else {"1"}
    adapter.build_source = lambda **kw: None
    await VKAdapter._handle_message_new(adapter, message, {"type": "message_new"})
    return handled


@pytest.mark.asyncio
async def test_an_accepted_message_is_marked_read_and_gets_a_reaction():
    adapter = make_adapter(reactions=ReactionConfig(processing=1))

    handled = await run_inbound(
        adapter,
        {"peer_id": PEER, "from_id": PEER, "text": "hi", "id": 7, "conversation_message_id": 3},
    )

    assert len(handled) == 1
    assert adapter.client.reads == [{"peer_id": PEER, "start_message_id": 7}]
    assert adapter.client.reactions == [{"peer_id": PEER, "cmid": 3, "reaction_id": 1}]


@pytest.mark.asyncio
async def test_an_unauthorized_sender_gets_no_read_receipt_and_no_reaction():
    """Acknowledging tells a stranger the bot is live and read their message."""
    adapter = make_adapter(reactions=ReactionConfig(processing=1))

    handled = await run_inbound(
        adapter,
        {"peer_id": PEER, "from_id": 999, "text": "hi", "id": 7, "conversation_message_id": 3},
        allowed=False,
    )

    assert handled == []
    assert adapter.client.reads == []
    assert adapter.client.reactions == []


@pytest.mark.asyncio
async def test_read_receipts_can_be_turned_off():
    adapter = make_adapter(mark_read_enabled=False)

    await run_inbound(
        adapter,
        {"peer_id": PEER, "from_id": PEER, "text": "hi", "id": 7, "conversation_message_id": 3},
    )

    assert adapter.client.reads == []


@pytest.mark.asyncio
async def test_a_successful_reply_closes_the_lifecycle_reaction():
    adapter = make_adapter(reactions=ReactionConfig(processing=1, done=16))
    adapter._cmid_by_anchor.set((PEER, "7"), 3)

    result = await VKAdapter.send(adapter, str(PEER), "the answer", reply_to="7")

    assert result.success
    assert adapter.client.reactions[-1]["reaction_id"] == 16


@pytest.mark.asyncio
async def test_replies_send_no_reactions_when_unconfigured():
    adapter = make_adapter()
    adapter._cmid_by_anchor.set((PEER, "7"), 3)

    await VKAdapter.send(adapter, str(PEER), "the answer", reply_to="7")

    assert adapter.client.reactions == []
    assert adapter.client.reaction_deletes == []
