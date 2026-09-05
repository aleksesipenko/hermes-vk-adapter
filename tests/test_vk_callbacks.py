"""Callback routing: actor-bound, expiring, replay-safe, retry-safe."""

from __future__ import annotations

import json

import pytest

from plugins.vk.callbacks import PICKER_KIND, VKCallbackRouter
from plugins.vk.interactive import InteractiveStore
from plugins.vk.keyboards import VKKeyboardFactory
from plugins.vk.utils import decode_callback_payload

OWNER = 987654321
OTHER = 42
PEER = 2_000_000_001
SESSION = "agent:main:vk:987654321"


class FakeClient:
    def __init__(self):
        self.answers = []
        self.edits = []
        self.messages = []

    async def send_message_event_answer(self, **kwargs):
        self.answers.append(kwargs)
        return 1

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)
        return 1

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return 1


def make_router(store=None, allow=True):
    client = FakeClient()
    router = VKCallbackRouter(
        client=client,
        is_allowed_user=lambda user_id: allow,
        keyboards=VKKeyboardFactory(),
        command_keyboard_enabled=False,
        store=store or InteractiveStore(),
    )
    return router, client


def event(payload, *, user_id=OWNER, peer_id=PEER, event_id="evt", cmid=11):
    return {
        "event_id": event_id,
        "user_id": user_id,
        "peer_id": peer_id,
        "conversation_message_id": cmid,
        "payload": payload,
    }


def last_answer(client):
    return client.answers[-1]["text"] if client.answers else None


# ── payload decoding ──────────────────────────────────────────────────────


def test_payload_decoding_rejects_junk_and_oversized_input():
    assert decode_callback_payload({"h": "ea"}) == {"h": "ea"}
    assert decode_callback_payload('{"h":"ea"}') == {"h": "ea"}
    assert decode_callback_payload("not json") == {}
    assert decode_callback_payload("[1,2,3]") == {}
    assert decode_callback_payload(None) == {}
    assert decode_callback_payload(json.dumps({"h": "ea", "pad": "x" * 400})) == {}


# ── approvals ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_originating_user_resolves_their_own_approval(monkeypatch):
    resolved = []
    monkeypatch.setattr(
        "tools.approval.resolve_gateway_approval",
        lambda session_key, choice: resolved.append((session_key, choice)),
    )
    store = InteractiveStore()
    store.register(kind="ea", action_id=1, user_id=OWNER, peer_id=PEER, session_key=SESSION)
    router, client = make_router(store)

    await router.handle(event({"h": "ea", "id": 1, "c": "once"}))

    assert resolved == [(SESSION, "once")]
    assert "Resolved: once" in last_answer(client)


@pytest.mark.asyncio
async def test_another_user_cannot_resolve_someone_elses_approval(monkeypatch):
    resolved = []
    monkeypatch.setattr(
        "tools.approval.resolve_gateway_approval",
        lambda session_key, choice: resolved.append((session_key, choice)),
    )
    store = InteractiveStore()
    store.register(kind="ea", action_id=1, user_id=OWNER, peer_id=PEER, session_key=SESSION)
    router, client = make_router(store)

    await router.handle(event({"h": "ea", "id": 1, "c": "once"}, user_id=OTHER, event_id="e2"))

    assert resolved == []
    assert "someone else" in last_answer(client)


@pytest.mark.asyncio
async def test_the_owner_cannot_resolve_from_a_different_peer(monkeypatch):
    resolved = []
    monkeypatch.setattr(
        "tools.approval.resolve_gateway_approval",
        lambda session_key, choice: resolved.append((session_key, choice)),
    )
    store = InteractiveStore()
    store.register(kind="ea", action_id=1, user_id=OWNER, peer_id=PEER, session_key=SESSION)
    router, client = make_router(store)

    await router.handle(event({"h": "ea", "id": 1, "c": "once"}, peer_id=555, event_id="e3"))

    assert resolved == []
    assert "another chat" in last_answer(client)


@pytest.mark.asyncio
async def test_a_user_outside_the_allowlist_is_refused_before_any_lookup(monkeypatch):
    resolved = []
    monkeypatch.setattr(
        "tools.approval.resolve_gateway_approval",
        lambda session_key, choice: resolved.append(1),
    )
    store = InteractiveStore()
    store.register(kind="ea", action_id=1, user_id=OWNER, peer_id=PEER, session_key=SESSION)
    router, client = make_router(store, allow=False)

    await router.handle(event({"h": "ea", "id": 1, "c": "once"}))

    assert resolved == []
    assert last_answer(client) == "Not authorized"


@pytest.mark.asyncio
async def test_an_unknown_approval_choice_never_reaches_hermes(monkeypatch):
    resolved = []
    monkeypatch.setattr(
        "tools.approval.resolve_gateway_approval",
        lambda session_key, choice: resolved.append(1),
    )
    store = InteractiveStore()
    store.register(kind="ea", action_id=1, user_id=OWNER, peer_id=PEER, session_key=SESSION)
    router, client = make_router(store)

    await router.handle(event({"h": "ea", "id": 1, "c": "sudo"}))

    assert resolved == []
    assert last_answer(client) == "Invalid approval"


@pytest.mark.asyncio
async def test_a_failed_resolver_leaves_the_approval_retryable(monkeypatch):
    attempts = []

    def flaky(session_key, choice):
        attempts.append(choice)
        if len(attempts) == 1:
            raise RuntimeError("gateway busy")

    monkeypatch.setattr("tools.approval.resolve_gateway_approval", flaky)
    store = InteractiveStore()
    store.register(kind="ea", action_id=1, user_id=OWNER, peer_id=PEER, session_key=SESSION)
    router, client = make_router(store)

    await router.handle(event({"h": "ea", "id": 1, "c": "once"}, event_id="try1"))
    assert "try again" in last_answer(client)

    await router.handle(event({"h": "ea", "id": 1, "c": "once"}, event_id="try2"))

    assert attempts == ["once", "once"]
    assert "Resolved: once" in last_answer(client)


@pytest.mark.asyncio
async def test_a_resolved_approval_cannot_be_replayed(monkeypatch):
    resolved = []
    monkeypatch.setattr(
        "tools.approval.resolve_gateway_approval",
        lambda session_key, choice: resolved.append(choice),
    )
    store = InteractiveStore()
    store.register(kind="ea", action_id=1, user_id=OWNER, peer_id=PEER, session_key=SESSION)
    router, client = make_router(store)

    await router.handle(event({"h": "ea", "id": 1, "c": "once"}, event_id="first"))
    await router.handle(event({"h": "ea", "id": 1, "c": "always"}, event_id="second"))

    assert resolved == ["once"]
    assert last_answer(client) == "Already resolved"


@pytest.mark.asyncio
async def test_the_same_vk_event_is_answered_at_most_once(monkeypatch):
    monkeypatch.setattr("tools.approval.resolve_gateway_approval", lambda *a: None)
    store = InteractiveStore()
    store.register(kind="ea", action_id=1, user_id=OWNER, peer_id=PEER, session_key=SESSION)
    router, client = make_router(store)

    await router.handle(event({"h": "ea", "id": 1, "c": "once"}, event_id="same"))
    await router.handle(event({"h": "ea", "id": 1, "c": "once"}, event_id="same"))

    assert len(client.answers) == 1


@pytest.mark.asyncio
async def test_malformed_and_unknown_payloads_are_refused():
    router, client = make_router()

    await router.handle(event("not json", event_id="a"))
    await router.handle(event({"h": "zz"}, event_id="b"))

    assert [answer["text"] for answer in client.answers] == [
        "Unsupported action",
        "Unsupported action",
    ]


@pytest.mark.asyncio
async def test_an_event_without_identity_is_dropped_silently():
    router, client = make_router()

    await router.handle(event({"h": "ea", "id": 1, "c": "once"}, user_id=0))

    assert client.answers == []


# ── clarify ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clarify_resolves_from_locally_stored_choices(monkeypatch):
    resolved = []
    monkeypatch.setattr(
        "tools.clarify_gateway.resolve_gateway_clarify",
        lambda clarify_id, response: resolved.append((clarify_id, response)),
    )
    store = InteractiveStore()
    store.register(
        kind="cl",
        action_id="c1",
        user_id=OWNER,
        peer_id=PEER,
        session_key=SESSION,
        data={"choices": ["alpha", "beta"]},
    )
    router, _ = make_router(store)

    await router.handle(event({"h": "cl", "id": "c1", "c": 1}))

    assert resolved == [("c1", "beta")]


@pytest.mark.asyncio
async def test_clarify_does_not_read_hermes_private_state(monkeypatch):
    """The old code reached into tools.clarify_gateway._entries."""
    import tools.clarify_gateway as clarify_gateway

    monkeypatch.setattr(
        "tools.clarify_gateway.resolve_gateway_clarify", lambda *a: None, raising=False
    )
    monkeypatch.delattr(clarify_gateway, "_entries", raising=False)

    store = InteractiveStore()
    store.register(
        kind="cl",
        action_id="c1",
        user_id=OWNER,
        peer_id=PEER,
        session_key=SESSION,
        data={"choices": ["alpha"]},
    )
    router, client = make_router(store)

    await router.handle(event({"h": "cl", "id": "c1", "c": 0}))

    assert last_answer(client) == "alpha"


@pytest.mark.asyncio
async def test_an_out_of_range_clarify_choice_is_refused(monkeypatch):
    resolved = []
    monkeypatch.setattr(
        "tools.clarify_gateway.resolve_gateway_clarify",
        lambda clarify_id, response: resolved.append(1),
    )
    store = InteractiveStore()
    store.register(
        kind="cl",
        action_id="c1",
        user_id=OWNER,
        peer_id=PEER,
        session_key=SESSION,
        data={"choices": ["alpha"]},
    )
    router, client = make_router(store)

    await router.handle(event({"h": "cl", "id": "c1", "c": 9}))

    assert resolved == []
    assert last_answer(client) == "Unknown choice"


@pytest.mark.asyncio
async def test_clarify_other_keeps_the_prompt_open(monkeypatch):
    awaiting = []
    monkeypatch.setattr(
        "tools.clarify_gateway.mark_awaiting_text", lambda clarify_id: awaiting.append(clarify_id)
    )
    store = InteractiveStore()
    store.register(
        kind="cl",
        action_id="c2",
        user_id=OWNER,
        peer_id=PEER,
        session_key=SESSION,
        data={"choices": ["alpha"]},
    )
    router, _ = make_router(store)

    await router.handle(event({"h": "cl", "id": "c2", "c": "other"}))

    assert awaiting == ["c2"]
    action, reason = store.authorize(kind="cl", action_id="c2", user_id=OWNER, peer_id=PEER)
    assert action is not None and reason is None


# ── model picker ──────────────────────────────────────────────────────────


def picker_store(on_selected=None):
    store = InteractiveStore()
    store.register(
        kind=PICKER_KIND,
        action_id="pick1",
        user_id=OWNER,
        peer_id=PEER,
        session_key=SESSION,
        data={
            "providers": [
                {
                    "slug": "openrouter",
                    "name": "OpenRouter",
                    "models": ["deepseek/chat", "qwen/chat"],
                }
            ],
            "on_model_selected": on_selected,
            "current_model": "deepseek/chat",
            "current_provider": "openrouter",
            "provider_page": 0,
        },
    )
    return store


@pytest.mark.asyncio
async def test_model_selection_calls_back_and_closes_the_picker():
    selected = []

    async def on_selected(chat_id, model_id, provider_slug):
        selected.append((chat_id, model_id, provider_slug))
        return f"Switched to {model_id}"

    store = picker_store(on_selected)
    router, client = make_router(store)

    await router.handle(event({"h": "mm", "i": "pick1", "p": 0, "pg": 0, "m": 1}))

    assert selected == [(str(PEER), "qwen/chat", "openrouter")]
    assert "Switched to qwen/chat" in client.edits[-1]["message"]


@pytest.mark.asyncio
async def test_another_user_cannot_drive_someone_elses_picker():
    selected = []

    async def on_selected(*args):
        selected.append(args)
        return "switched"

    store = picker_store(on_selected)
    router, client = make_router(store)

    await router.handle(event({"h": "mm", "i": "pick1", "p": 0, "pg": 0, "m": 0}, user_id=OTHER))

    assert selected == []
    assert last_answer(client) == "Picker expired"


@pytest.mark.asyncio
async def test_picker_navigation_requires_a_known_picker_id():
    store = picker_store()
    router, client = make_router(store)

    await router.handle(event({"h": "mb", "i": "unknown"}))

    assert client.edits == []
    assert last_answer(client) == "Picker expired"


@pytest.mark.asyncio
async def test_picker_back_renders_providers_and_close_clears_the_keyboard():
    store = picker_store()
    router, client = make_router(store)

    await router.handle(event({"h": "mb", "i": "pick1"}, event_id="back"))
    assert "Choose provider" in client.edits[-1]["message"]

    await router.handle(event({"h": "mc", "i": "pick1"}, event_id="close"))
    assert client.edits[-1]["keyboard"] is None
    assert "closed" in client.edits[-1]["message"].lower()

    action, _ = store.authorize(kind=PICKER_KIND, action_id="pick1", user_id=OWNER, peer_id=PEER)
    assert action is None


@pytest.mark.asyncio
async def test_an_unknown_model_index_is_refused():
    selected = []

    async def on_selected(*args):
        selected.append(args)

    store = picker_store(on_selected)
    router, client = make_router(store)

    await router.handle(event({"h": "mm", "i": "pick1", "p": 0, "pg": 0, "m": 99}))

    assert selected == []
    assert last_answer(client) == "Unknown model"


# ── logging hygiene ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rejection_logs_carry_no_session_key_or_prompt_text(caplog, monkeypatch):
    store = InteractiveStore()
    store.register(
        kind="cl",
        action_id="c1",
        user_id=OWNER,
        peer_id=PEER,
        session_key=SESSION,
        data={"choices": ["a private answer"]},
    )
    router, _ = make_router(store)

    with caplog.at_level("DEBUG"):
        await router.handle(event({"h": "cl", "id": "c1", "c": 0}, user_id=OTHER))

    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert SESSION not in blob
    assert "a private answer" not in blob
