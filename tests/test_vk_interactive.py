"""Interactive callback state must be actor-bound, expiring and replay-safe."""

from __future__ import annotations

import pytest

from plugins.vk.interactive import (
    REJECT_EXPIRED,
    REJECT_FOREIGN_PEER,
    REJECT_FOREIGN_USER,
    REJECT_REPLAY,
    REJECT_RESOLVED,
    REJECT_UNKNOWN,
    InteractiveStore,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_store(clock=None, **kwargs):
    return InteractiveStore(clock=clock or FakeClock(), **kwargs)


def register(store, **overrides):
    fields = {
        "kind": "ea",
        "action_id": "1",
        "user_id": 987654321,
        "peer_id": 2_000_000_001,
        "session_key": "agent:main:vk:987654321",
    }
    fields.update(overrides)
    return store.register(**fields)


# ── binding ───────────────────────────────────────────────────────────────


def test_the_originating_actor_is_authorized():
    store = make_store()
    register(store)

    action, reason = store.authorize(
        kind="ea", action_id="1", user_id=987654321, peer_id=2_000_000_001
    )

    assert reason is None
    assert action is not None
    assert action.session_key == "agent:main:vk:987654321"


def test_another_user_in_the_same_chat_cannot_resolve_it():
    """A second allowlisted user must not answer someone else's prompt."""
    store = make_store()
    register(store)

    action, reason = store.authorize(kind="ea", action_id="1", user_id=42, peer_id=2_000_000_001)

    assert action is None
    assert reason == REJECT_FOREIGN_USER


def test_the_same_user_cannot_resolve_it_from_another_peer():
    store = make_store()
    register(store)

    action, reason = store.authorize(kind="ea", action_id="1", user_id=987654321, peer_id=555)

    assert action is None
    assert reason == REJECT_FOREIGN_PEER


def test_an_unknown_action_is_rejected():
    store = make_store()

    action, reason = store.authorize(kind="ea", action_id="nope", user_id=1, peer_id=1)

    assert action is None
    assert reason == REJECT_UNKNOWN


def test_kinds_do_not_collide_on_the_same_id():
    store = make_store()
    register(store, kind="ea", action_id="1")
    register(store, kind="sc", action_id="1", user_id=5, peer_id=6, session_key="other")

    approval, _ = store.authorize(
        kind="ea", action_id="1", user_id=987654321, peer_id=2_000_000_001
    )
    confirm, _ = store.authorize(kind="sc", action_id="1", user_id=5, peer_id=6)

    assert approval.session_key == "agent:main:vk:987654321"
    assert confirm.session_key == "other"


@pytest.mark.parametrize("bad", [None, "", "  ", 0])
def test_malformed_identifiers_are_rejected(bad):
    store = make_store()
    register(store)

    action, reason = store.authorize(
        kind="ea", action_id=bad, user_id=987654321, peer_id=2_000_000_001
    )

    assert action is None
    assert reason == REJECT_UNKNOWN


# ── expiry ────────────────────────────────────────────────────────────────


def test_state_expires_and_stops_resolving():
    clock = FakeClock()
    store = make_store(clock, ttl_seconds=900)
    register(store)

    clock.advance(899)
    still_live = store.authorize(
        kind="ea", action_id="1", user_id=987654321, peer_id=2_000_000_001
    )
    assert still_live[1] is None

    clock.advance(2)
    action, reason = store.authorize(
        kind="ea", action_id="1", user_id=987654321, peer_id=2_000_000_001
    )
    assert action is None
    assert reason == REJECT_EXPIRED


def test_store_is_bounded():
    store = make_store(max_entries=3)
    for index in range(6):
        register(store, action_id=str(index))

    live = [
        index
        for index in range(6)
        if store.authorize(
            kind="ea", action_id=str(index), user_id=987654321, peer_id=2_000_000_001
        )[0]
    ]
    assert len(live) <= 3


# ── resolution lifecycle ──────────────────────────────────────────────────


def test_state_survives_until_it_is_explicitly_discarded():
    """A failed resolver must leave the button usable until it expires."""
    store = make_store()
    register(store)

    for _ in range(3):
        action, reason = store.authorize(
            kind="ea", action_id="1", user_id=987654321, peer_id=2_000_000_001
        )
        assert reason is None and action is not None

    store.discard("ea", "1")

    action, reason = store.authorize(
        kind="ea", action_id="1", user_id=987654321, peer_id=2_000_000_001
    )
    assert action is None
    assert reason == REJECT_RESOLVED


def test_discarding_an_unknown_action_is_harmless():
    store = make_store()
    store.discard("ea", "missing")


# ── replay ────────────────────────────────────────────────────────────────


def test_a_vk_event_id_can_only_be_claimed_once():
    store = make_store()

    assert store.claim_event("evt-1") is True
    assert store.claim_event("evt-1") is False
    assert store.claim_event("evt-2") is True


def test_an_absent_event_id_is_never_treated_as_a_replay():
    store = make_store()

    assert store.claim_event("") is True
    assert store.claim_event("") is True


def test_replay_rejection_reason_is_available():
    assert REJECT_REPLAY == "replay"


# ── stored data ───────────────────────────────────────────────────────────


def test_arbitrary_action_data_round_trips():
    store = make_store()
    register(store, kind="cl", action_id="c1", data={"choices": ["alpha", "beta"]})

    action, _ = store.authorize(
        kind="cl", action_id="c1", user_id=987654321, peer_id=2_000_000_001
    )

    assert action.data["choices"] == ["alpha", "beta"]


def test_repr_never_leaks_the_session_key_or_stored_data():
    store = make_store()
    action = register(store, data={"secret_text": "user private message"})

    rendered = f"{action!r} {action!s}"

    assert "agent:main:vk:987654321" not in rendered
    assert "user private message" not in rendered
