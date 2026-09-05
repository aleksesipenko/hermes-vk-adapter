"""Bounded, actor-bound state for VK interactive prompts.

An approval, a slash confirmation, a clarify question and the model picker all
have the same security shape: the gateway hands us a session key, we render
buttons, and some time later a VK callback claims to be the answer.  Nothing in
the callback proves who pressed the button except the ``user_id`` and
``peer_id`` VK attaches to the event, so the answer is only trustworthy if the
prompt recorded who it was for.

Without that binding, any user allowed to talk to the bot could resolve another
user's pending approval by pressing a button in a shared conversation -- the
approval prompt is exactly the surface where that matters most.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .state import BoundedTTLCache

#: Why an inbound callback was refused. Kept as codes so the caller decides the
#: user-facing wording and nothing internal leaks into a VK snackbar.
REJECT_UNKNOWN = "unknown"
REJECT_EXPIRED = "expired"
REJECT_FOREIGN_USER = "foreign_user"
REJECT_FOREIGN_PEER = "foreign_peer"
REJECT_REPLAY = "replay"
REJECT_RESOLVED = "resolved"

DEFAULT_MAX_ACTIONS = 256
DEFAULT_ACTION_TTL_SECONDS = 900.0
DEFAULT_MAX_SEEN_EVENTS = 512
DEFAULT_SEEN_EVENT_TTL_SECONDS = 900.0


@dataclass(frozen=True)
class PendingAction:
    """One outstanding interactive prompt, bound to who may answer it."""

    kind: str
    action_id: str
    user_id: int
    peer_id: int
    session_key: str = field(repr=False)
    data: dict[str, Any] = field(default_factory=dict, repr=False)
    created_at: float = 0.0
    message_id: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.kind}:{self.action_id}"


class InteractiveStore:
    """Bounded, expiring registry of pending interactive prompts.

    Entries are removed only when the caller explicitly discards them after a
    successful terminal resolution.  A resolver that raises therefore leaves the
    button usable until the entry expires, instead of consuming the state and
    stranding the user with a prompt nothing can answer.
    """

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ACTIONS,
        ttl_seconds: float = DEFAULT_ACTION_TTL_SECONDS,
        max_seen_events: int = DEFAULT_MAX_SEEN_EVENTS,
        seen_event_ttl_seconds: float = DEFAULT_SEEN_EVENT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._actions = BoundedTTLCache(
            max_entries=max_entries, ttl_seconds=ttl_seconds, clock=clock
        )
        # Tombstones outlive the entries themselves so a second press can be
        # told *why* nothing happened -- timed out, already answered, or never
        # ours at all -- instead of a flat "unknown".
        self._tombstones = BoundedTTLCache(
            max_entries=max_entries, ttl_seconds=ttl_seconds * 4, clock=clock
        )
        self._seen_events = BoundedTTLCache(
            max_entries=max_seen_events, ttl_seconds=seen_event_ttl_seconds, clock=clock
        )

    def register(
        self,
        *,
        kind: str,
        action_id: Any,
        user_id: int,
        peer_id: int,
        session_key: str,
        data: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> PendingAction:
        action = PendingAction(
            kind=str(kind),
            action_id=str(action_id),
            user_id=int(user_id),
            peer_id=int(peer_id),
            session_key=str(session_key),
            data=dict(data or {}),
            created_at=self._clock(),
            message_id=message_id,
        )
        key = self._key(action.kind, action.action_id)
        self._actions.set(key, action)
        self._tombstones.set(key, REJECT_EXPIRED)
        return action

    def authorize(
        self,
        *,
        kind: str,
        action_id: Any,
        user_id: int,
        peer_id: int,
    ) -> tuple[PendingAction | None, str | None]:
        """Return the action when this actor may resolve it, else a reason."""
        if action_id is None or not str(action_id).strip():
            return None, REJECT_UNKNOWN
        key = self._key(str(kind), str(action_id))
        action = self._actions.get(key)
        if action is None:
            return None, self._tombstones.get(key, REJECT_UNKNOWN)
        if action.user_id != int(user_id or 0):
            return None, REJECT_FOREIGN_USER
        if action.peer_id != int(peer_id or 0):
            return None, REJECT_FOREIGN_PEER
        return action, None

    def discard(self, kind: str, action_id: Any) -> None:
        """Forget an action after it has actually been resolved."""
        key = self._key(str(kind), str(action_id))
        if self._actions.pop(key, None) is not None:
            self._tombstones.set(key, REJECT_RESOLVED)

    def claim_event(self, event_id: str) -> bool:
        """Claim a VK ``event_id`` so it is answered at most once.

        An event without an id cannot be tracked, and refusing it would break
        the interaction, so it is always allowed through.
        """
        if not event_id:
            return True
        if event_id in self._seen_events:
            return False
        self._seen_events.set(event_id, True)
        return True

    @staticmethod
    def _key(kind: str, action_id: str) -> tuple[str, str]:
        return (kind, action_id)
