"""Bounded TTL storage shared by the VK adapter's in-memory state."""

from __future__ import annotations

import pytest

from plugins.vk.state import BoundedTTLCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_get_returns_default_for_missing_key():
    cache = BoundedTTLCache(max_entries=4, ttl_seconds=60, clock=FakeClock())

    assert cache.get("nope") is None
    assert cache.get("nope", "fallback") == "fallback"


def test_entries_expire_after_ttl():
    clock = FakeClock()
    cache = BoundedTTLCache(max_entries=4, ttl_seconds=60, clock=clock)
    cache.set("k", "v")

    clock.advance(59)
    assert cache.get("k") == "v"

    clock.advance(2)
    assert cache.get("k") is None
    assert "k" not in cache


def test_size_cap_evicts_oldest_first():
    cache = BoundedTTLCache(max_entries=2, ttl_seconds=60, clock=FakeClock())
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)

    assert len(cache) == 2
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3


def test_expired_entries_are_evicted_before_live_ones():
    clock = FakeClock()
    cache = BoundedTTLCache(max_entries=2, ttl_seconds=60, clock=clock)
    cache.set("stale", 1)
    clock.advance(61)
    cache.set("fresh", 2)
    cache.set("newest", 3)

    assert cache.get("stale") is None
    assert cache.get("fresh") == 2
    assert cache.get("newest") == 3


def test_setdefault_creates_once_and_reuses_within_ttl():
    clock = FakeClock()
    cache = BoundedTTLCache(max_entries=4, ttl_seconds=60, clock=clock)
    calls = []

    def factory():
        calls.append(1)
        return len(calls)

    assert cache.setdefault("k", factory) == 1
    assert cache.setdefault("k", factory) == 1
    assert len(calls) == 1

    clock.advance(61)
    assert cache.setdefault("k", factory) == 2
    assert len(calls) == 2


def test_set_refreshes_recency_but_not_expiry_of_other_keys():
    clock = FakeClock()
    cache = BoundedTTLCache(max_entries=2, ttl_seconds=60, clock=clock)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("a", 10)
    cache.set("c", 3)

    assert cache.get("a") == 10
    assert cache.get("b") is None
    assert cache.get("c") == 3


def test_pop_removes_and_returns():
    cache = BoundedTTLCache(max_entries=4, ttl_seconds=60, clock=FakeClock())
    cache.set("k", "v")

    assert cache.pop("k") == "v"
    assert cache.pop("k", "gone") == "gone"
    assert len(cache) == 0


def test_rejects_nonsense_configuration():
    with pytest.raises(ValueError):
        BoundedTTLCache(max_entries=0, ttl_seconds=60)
    with pytest.raises(ValueError):
        BoundedTTLCache(max_entries=4, ttl_seconds=0)
