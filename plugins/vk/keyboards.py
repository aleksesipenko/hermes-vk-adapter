"""VK keyboard JSON builders."""

from __future__ import annotations

import json
from typing import Any

try:
    from vkbottle import Callback, Keyboard, KeyboardButtonColor, Text
except Exception:  # pragma: no cover - raw JSON fallback is supported.
    Callback = None
    Keyboard = None
    KeyboardButtonColor = None
    Text = None

PROVIDER_PAGE_SIZE = 6
MODEL_PAGE_SIZE = 6


class VKKeyboardFactory:
    def command_keyboard(self) -> str:
        return self._keyboard_json(
            [
                [{"type": "text", "label": "/commands", "color": "primary"}],
                [
                    {"type": "text", "label": "/status", "color": "secondary"},
                    {"type": "text", "label": "/model", "color": "secondary"},
                ],
                [
                    {"type": "text", "label": "/new", "color": "secondary"},
                    {"type": "text", "label": "/stop", "color": "negative"},
                ],
            ],
            inline=False,
        )

    def approval_keyboard(self, approval_id: int) -> str:
        return self._keyboard_json(
            [
                [
                    {"type": "callback", "label": "Allow once", "payload": {"h": "ea", "id": approval_id, "c": "once"}},
                    {"type": "callback", "label": "Session", "payload": {"h": "ea", "id": approval_id, "c": "session"}},
                ],
                [
                    {"type": "callback", "label": "Always", "payload": {"h": "ea", "id": approval_id, "c": "always"}},
                    {"type": "callback", "label": "Deny", "payload": {"h": "ea", "id": approval_id, "c": "deny"}},
                ],
            ],
            inline=True,
        )

    def slash_confirm_keyboard(self, confirm_id: str) -> str:
        return self._keyboard_json(
            [
                [
                    {"type": "callback", "label": "Approve once", "payload": {"h": "sc", "id": confirm_id, "c": "once"}},
                    {"type": "callback", "label": "Always", "payload": {"h": "sc", "id": confirm_id, "c": "always"}},
                ],
                [{"type": "callback", "label": "Cancel", "payload": {"h": "sc", "id": confirm_id, "c": "cancel"}}],
            ],
            inline=True,
        )

    def clarify_keyboard(self, choices: list, clarify_id: str) -> str:
        buttons = [
            {"type": "callback", "label": str(index + 1), "payload": {"h": "cl", "id": clarify_id, "c": index}}
            for index, _choice in enumerate(choices[:8])
        ]
        buttons.append({"type": "callback", "label": "Other", "payload": {"h": "cl", "id": clarify_id, "c": "other"}})
        rows = _chunk_buttons(buttons, size=2)
        return self._keyboard_json(rows, inline=True)

    def provider_keyboard(self, providers: list, page: int = 0) -> str:
        page = _bounded_page(page, len(providers), PROVIDER_PAGE_SIZE)
        start = page * PROVIDER_PAGE_SIZE
        visible = providers[start : start + PROVIDER_PAGE_SIZE]
        buttons = []
        for offset, _provider in enumerate(visible):
            buttons.append(
                {
                    "type": "callback",
                    "label": str(offset + 1),
                    "payload": {"h": "mpc", "pg": page, "p": start + offset},
                }
            )
        rows = _chunk_buttons(buttons, size=3)
        nav = [{"type": "callback", "label": "Close", "payload": {"h": "mc"}}]
        if page > 0:
            nav.append({"type": "callback", "label": "Prev", "payload": {"h": "mp", "pg": page - 1}})
        if start + PROVIDER_PAGE_SIZE < len(providers):
            nav.append({"type": "callback", "label": "Next", "payload": {"h": "mp", "pg": page + 1}})
        rows.extend(_chunk_buttons(nav, size=3))
        return self._keyboard_json(rows, inline=True)

    def model_keyboard(self, provider: dict[str, Any], provider_index: int, page: int = 0) -> str:
        models = provider.get("models") or []
        page = _bounded_page(page, len(models), MODEL_PAGE_SIZE)
        start = page * MODEL_PAGE_SIZE
        visible = models[start : start + MODEL_PAGE_SIZE]
        rows = []
        buttons = []
        for offset, _model_id in enumerate(visible):
            buttons.append(
                {
                    "type": "callback",
                    "label": str(offset + 1),
                    "payload": {"h": "mm", "p": provider_index, "pg": page, "m": start + offset},
                }
            )
        rows.extend(_chunk_buttons(buttons, size=3))
        nav = [{"type": "callback", "label": "Back", "payload": {"h": "mb"}}]
        if page > 0:
            nav.append({"type": "callback", "label": "Prev", "payload": {"h": "mmp", "p": provider_index, "pg": page - 1}})
        if start + MODEL_PAGE_SIZE < len(models):
            nav.append({"type": "callback", "label": "Next", "payload": {"h": "mmp", "p": provider_index, "pg": page + 1}})
        rows.extend(_chunk_buttons(nav, size=3))
        rows.append([{"type": "callback", "label": "Close", "payload": {"h": "mc"}}])
        return self._keyboard_json(rows, inline=True)

    def _keyboard_json(self, rows: list[list[dict[str, Any]]], *, inline: bool) -> str:
        if Keyboard is not None and Text is not None and Callback is not None:
            keyboard = Keyboard(one_time=False, inline=inline)
            for row_index, row in enumerate(rows):
                if row_index:
                    keyboard.row()
                for button in row:
                    if button["type"] == "callback":
                        keyboard.add(Callback(button["label"], payload=button["payload"]))
                    else:
                        color_name = button.get("color", "secondary").upper()
                        color = getattr(KeyboardButtonColor, color_name, None) if KeyboardButtonColor else None
                        keyboard.add(Text(button["label"]), color=color)
            return keyboard.get_json()

        buttons = []
        for row in rows:
            rendered = []
            for button in row:
                action = {"type": button["type"], "label": button["label"]}
                if "payload" in button:
                    action["payload"] = button["payload"]
                item = {"action": action}
                if "color" in button:
                    item["color"] = button["color"]
                rendered.append(item)
            buttons.append(rendered)
        return json.dumps({"one_time": False, "inline": inline, "buttons": buttons}, ensure_ascii=False, separators=(",", ":"))


def model_picker_provider_text(
    providers: list,
    current_model: str,
    current_provider: str,
    page: int = 0,
) -> str:
    page = _bounded_page(page, len(providers), PROVIDER_PAGE_SIZE)
    total_pages = _page_count(len(providers), PROVIDER_PAGE_SIZE)
    start = page * PROVIDER_PAGE_SIZE
    visible = providers[start : start + PROVIDER_PAGE_SIZE]
    lines = [
        "Model configuration",
        f"Current model: {current_model or 'unknown'}",
        f"Provider: {current_provider or 'unknown'}",
        "",
        f"Choose provider: page {page + 1}/{total_pages}",
    ]
    for index, provider in enumerate(visible, start=1):
        lines.append(f"{index}. {provider.get('name') or provider.get('slug')}")
    return "\n".join(lines)


def model_picker_model_text(provider: dict[str, Any], page: int = 0) -> str:
    models = provider.get("models") or []
    page = _bounded_page(page, len(models), MODEL_PAGE_SIZE)
    total_pages = _page_count(len(models), MODEL_PAGE_SIZE)
    start = page * MODEL_PAGE_SIZE
    visible = models[start : start + MODEL_PAGE_SIZE]
    lines = [
        f"Provider: {provider.get('name') or provider.get('slug')}",
        "",
        f"Select a model: page {page + 1}/{total_pages}",
    ]
    for index, model_id in enumerate(visible, start=1):
        lines.append(f"{index}. {model_id}")
    if len(models) > MODEL_PAGE_SIZE:
        lines.append("More models are available by typing /model <model-id>.")
    return "\n".join(lines)


def _chunk_buttons(buttons: list[dict[str, Any]], *, size: int) -> list[list[dict[str, Any]]]:
    return [buttons[index : index + size] for index in range(0, len(buttons), size)]


def _page_count(total: int, page_size: int) -> int:
    return max(1, (total + page_size - 1) // page_size)


def _bounded_page(page: int, total: int, page_size: int) -> int:
    return max(0, min(int(page or 0), _page_count(total, page_size) - 1))
