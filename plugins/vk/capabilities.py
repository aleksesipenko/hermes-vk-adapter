"""What the VK client on the other end can actually render.

Every ``message_new`` carries a ``client_info`` block describing the sending
client's button support.  Sending a callback keyboard to a client that cannot
render one leaves the user with a prompt they can see but not answer, so the
adapter negotiates instead of assuming.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ClientCapabilities:
    """Button support reported by one VK client.

    The defaults are the conservative shape: plain text keyboards only. An
    unknown or malformed ``client_info`` therefore degrades to the fallback
    rendering rather than to a prompt the user cannot use.
    """

    supports_keyboard: bool = True
    supports_inline_keyboard: bool = False
    supports_callback: bool = False
    supports_carousel: bool = False

    @classmethod
    def from_client_info(cls, client_info: Any) -> ClientCapabilities:
        if not isinstance(client_info, dict):
            return cls()
        actions = client_info.get("button_actions")
        actions = {str(action) for action in actions} if isinstance(actions, list) else set()
        return cls(
            supports_keyboard=bool(client_info.get("keyboard", True)),
            supports_inline_keyboard=bool(client_info.get("inline_keyboard", False)),
            supports_callback="callback" in actions,
            supports_carousel=bool(client_info.get("carousel", False)),
        )
