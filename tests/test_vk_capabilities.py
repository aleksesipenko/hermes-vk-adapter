"""client_info negotiation and bounded identity resolution."""

from __future__ import annotations

import pytest

from plugins.vk.capabilities import ClientCapabilities

MODERN = {
    "button_actions": ["text", "vkpay", "open_app", "location", "open_link", "callback"],
    "keyboard": True,
    "inline_keyboard": True,
    "carousel": True,
    "lang_id": 0,
}
OLD = {"button_actions": ["text"], "keyboard": True, "inline_keyboard": False}


def test_a_modern_client_supports_callbacks_and_inline_keyboards():
    caps = ClientCapabilities.from_client_info(MODERN)

    assert caps.supports_callback is True
    assert caps.supports_inline_keyboard is True
    assert caps.supports_keyboard is True


def test_an_old_client_reports_text_buttons_only():
    caps = ClientCapabilities.from_client_info(OLD)

    assert caps.supports_callback is False
    assert caps.supports_inline_keyboard is False
    assert caps.supports_keyboard is True


@pytest.mark.parametrize("payload", [None, {}, "garbage", [], {"button_actions": "text"}])
def test_missing_or_malformed_client_info_falls_back_to_the_safe_shape(payload):
    """Unknown client: assume only what every VK client has had for years."""
    caps = ClientCapabilities.from_client_info(payload)

    assert caps.supports_callback is False
    assert caps.supports_inline_keyboard is False
    assert caps.supports_keyboard is True


def test_capabilities_are_hashable_and_comparable():
    modern = ClientCapabilities.from_client_info(MODERN)
    old = ClientCapabilities.from_client_info(OLD)

    assert modern == ClientCapabilities.from_client_info(MODERN)
    assert modern != old
