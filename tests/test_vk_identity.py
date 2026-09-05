"""Capability caching, keyboard degradation, and identity resolution."""

from __future__ import annotations

import pytest

from plugins.vk.adapter import VKAdapter
from plugins.vk.state import BoundedTTLCache

pytestmark = pytest.mark.hermes_contract

DM_PEER = 987654321
GROUP_PEER = 2_000_000_001

MODERN = {"button_actions": ["text", "callback"], "keyboard": True, "inline_keyboard": True}
OLD = {"button_actions": ["text"], "keyboard": True, "inline_keyboard": False}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeClient:
    def __init__(self, *, users=None, conversations=None, error=None):
        self._users = users
        self._conversations = conversations
        self._error = error
        self.user_calls = 0
        self.conversation_calls = 0

    async def get_users(self, user_ids):
        self.user_calls += 1
        if self._error:
            raise self._error
        return self._users or []

    async def get_conversation(self, peer_id):
        self.conversation_calls += 1
        if self._error:
            raise self._error
        return self._conversations


def make_adapter(client=None, clock=None):
    clock = clock or FakeClock()
    adapter = object.__new__(VKAdapter)
    adapter.client = client or FakeClient()
    adapter.group_id = 123456789
    adapter._capabilities = BoundedTTLCache(max_entries=4, ttl_seconds=600, clock=clock)
    adapter._identities = BoundedTTLCache(max_entries=4, ttl_seconds=600, clock=clock)
    adapter._clock = clock
    return adapter


# ── capability cache ──────────────────────────────────────────────────────


def test_capabilities_are_remembered_per_peer():
    adapter = make_adapter()

    VKAdapter._remember_capabilities(adapter, DM_PEER, MODERN)

    assert VKAdapter._peer_capabilities(adapter, DM_PEER).supports_callback is True
    assert VKAdapter._peer_capabilities(adapter, GROUP_PEER).supports_callback is False


def test_capability_cache_expires():
    clock = FakeClock()
    adapter = make_adapter(clock=clock)
    VKAdapter._remember_capabilities(adapter, DM_PEER, MODERN)

    clock.advance(601)

    assert VKAdapter._peer_capabilities(adapter, DM_PEER).supports_callback is False


def test_capability_cache_is_bounded():
    adapter = make_adapter()
    for peer in range(10):
        VKAdapter._remember_capabilities(adapter, peer, MODERN)

    assert len(adapter._capabilities) <= 4


def test_an_absent_client_info_does_not_erase_a_known_capability():
    """A later event without client_info must not downgrade a modern client."""
    adapter = make_adapter()
    VKAdapter._remember_capabilities(adapter, DM_PEER, MODERN)

    VKAdapter._remember_capabilities(adapter, DM_PEER, None)

    assert VKAdapter._peer_capabilities(adapter, DM_PEER).supports_callback is True


# ── keyboard degradation ──────────────────────────────────────────────────


def test_callback_keyboards_are_sent_only_to_clients_that_support_them():
    adapter = make_adapter()
    VKAdapter._remember_capabilities(adapter, DM_PEER, MODERN)
    VKAdapter._remember_capabilities(adapter, GROUP_PEER, OLD)

    assert VKAdapter._callbacks_supported(adapter, DM_PEER) is True
    assert VKAdapter._callbacks_supported(adapter, GROUP_PEER) is False


def test_approval_falls_back_to_typed_instructions_for_old_clients():
    adapter = make_adapter()
    VKAdapter._remember_capabilities(adapter, GROUP_PEER, OLD)

    text = VKAdapter._approval_fallback_text(adapter)

    assert "/approve" in text
    for choice in ("once", "session", "always", "deny"):
        assert choice in text.lower()


def test_clarify_falls_back_to_a_numbered_list_for_old_clients():
    adapter = make_adapter()

    text = VKAdapter._clarify_fallback_text(adapter, ["alpha", "beta", "gamma"])

    assert "1. alpha" in text
    assert "3. gamma" in text
    assert "reply" in text.lower()


# ── identity resolution ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_dm_user_gets_their_real_name():
    client = FakeClient(users=[{"id": DM_PEER, "first_name": "Ада", "last_name": "Лавлейс"}])
    adapter = make_adapter(client)

    assert await VKAdapter._resolve_user_name(adapter, DM_PEER) == "Ада Лавлейс"


@pytest.mark.asyncio
async def test_a_resolved_name_is_cached_and_not_refetched():
    client = FakeClient(users=[{"id": DM_PEER, "first_name": "Ада", "last_name": "Лавлейс"}])
    adapter = make_adapter(client)

    await VKAdapter._resolve_user_name(adapter, DM_PEER)
    await VKAdapter._resolve_user_name(adapter, DM_PEER)

    assert client.user_calls == 1


@pytest.mark.asyncio
async def test_a_failed_lookup_degrades_to_a_numeric_label():
    adapter = make_adapter(FakeClient(error=RuntimeError("no users scope")))

    assert await VKAdapter._resolve_user_name(adapter, DM_PEER) == f"VK user {DM_PEER}"


@pytest.mark.asyncio
async def test_a_failed_lookup_is_not_cached_forever_as_a_success():
    client = FakeClient(error=RuntimeError("transient"))
    adapter = make_adapter(client)

    await VKAdapter._resolve_user_name(adapter, DM_PEER)
    client._error = None
    client._users = [{"id": DM_PEER, "first_name": "Ада", "last_name": "Лавлейс"}]

    assert await VKAdapter._resolve_user_name(adapter, DM_PEER) == "Ада Лавлейс"


@pytest.mark.asyncio
async def test_a_group_chat_gets_its_real_title():
    client = FakeClient(
        conversations={
            "items": [
                {
                    "peer": {"id": GROUP_PEER, "type": "chat"},
                    "chat_settings": {"title": "Команда Hermes"},
                }
            ]
        }
    )
    adapter = make_adapter(client)

    assert await VKAdapter._resolve_chat_name(adapter, GROUP_PEER) == "Команда Hermes"


@pytest.mark.asyncio
async def test_a_missing_chat_title_degrades_to_a_numeric_label():
    adapter = make_adapter(FakeClient(conversations={"items": []}))

    assert await VKAdapter._resolve_chat_name(adapter, GROUP_PEER) == f"VK peer {GROUP_PEER}"


@pytest.mark.asyncio
async def test_a_dm_chat_name_reuses_the_user_lookup_instead_of_a_second_call():
    client = FakeClient(users=[{"id": DM_PEER, "first_name": "Ада", "last_name": "Лавлейс"}])
    adapter = make_adapter(client)

    assert await VKAdapter._resolve_chat_name(adapter, DM_PEER) == "Ада Лавлейс"
    assert client.conversation_calls == 0


@pytest.mark.asyncio
async def test_identity_lookup_never_blocks_the_message_on_failure():
    class Hanging(FakeClient):
        async def get_users(self, user_ids):
            raise TimeoutError("VK slow")

    adapter = make_adapter(Hanging())

    assert await VKAdapter._resolve_user_name(adapter, DM_PEER) == f"VK user {DM_PEER}"
