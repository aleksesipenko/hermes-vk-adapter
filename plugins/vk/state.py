"""Bounded, expiring in-memory state for the VK adapter.

Every piece of state this adapter keeps between events -- outbound idempotency
keys, recently seen Long Poll events, pending interactive callbacks, resolved
identities, client capabilities -- has the same two requirements: it must not
grow without bound in a long-running gateway, and a stale entry must stop being
trusted after a while.  One small primitive covers all of them, so the adapter
needs neither a cache library nor a database.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any


class BoundedTTLCache:
    """Insertion-ordered cache with a hard entry cap and a per-entry TTL.

    Expired entries are dropped lazily on access and eagerly when space is
    needed, so a burst of short-lived keys cannot evict live ones while dead
    ones linger.  ``clock`` is injectable to keep expiry testable without
    sleeping; it must be monotonic.
    """

    __slots__ = ("_clock", "_entries", "_max_entries", "_ttl")

    def __init__(
        self,
        *,
        max_entries: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries!r}")
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be > 0, got {ttl_seconds!r}")
        self._max_entries = int(max_entries)
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._entries: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()

    def __len__(self) -> int:
        self.purge()
        return len(self._entries)

    def __contains__(self, key: Hashable) -> bool:
        return self._live(key) is not None

    def get(self, key: Hashable, default: Any = None) -> Any:
        entry = self._live(key)
        return default if entry is None else entry[1]

    def set(self, key: Hashable, value: Any) -> None:
        self._entries.pop(key, None)
        self._make_room()
        self._entries[key] = (self._clock() + self._ttl, value)

    def setdefault(self, key: Hashable, factory: Callable[[], Any]) -> Any:
        """Return the live value for ``key``, creating it via ``factory`` once."""
        entry = self._live(key)
        if entry is not None:
            return entry[1]
        value = factory()
        self.set(key, value)
        return value

    def pop(self, key: Hashable, default: Any = None) -> Any:
        entry = self._live(key)
        self._entries.pop(key, None)
        return default if entry is None else entry[1]

    def purge(self) -> None:
        """Drop every expired entry."""
        now = self._clock()
        for key in [key for key, (expires, _) in self._entries.items() if expires <= now]:
            self._entries.pop(key, None)

    def _live(self, key: Hashable) -> tuple[float, Any] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry[0] <= self._clock():
            self._entries.pop(key, None)
            return None
        return entry

    def _make_room(self) -> None:
        if len(self._entries) < self._max_entries:
            return
        self.purge()
        while len(self._entries) >= self._max_entries:
            self._entries.popitem(last=False)
