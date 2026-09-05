"""VK keyboard JSON builders."""

from __future__ import annotations

import json
from typing import Any

PROVIDER_PAGE_SIZE = 6
MODEL_PAGE_SIZE = 6


def _cb(label: str, **payload: Any) -> dict[str, Any]:
    """A VK ``callback`` button. Payload keys are kept short — VK caps it."""
    return {"type": "callback", "label": label, "payload": payload}


def _txt(label: str, color: str = "secondary") -> dict[str, Any]:
    return {"type": "text", "label": label, "color": color}


class VKKeyboardFactory:
    def command_keyboard(self) -> str:
        return self._keyboard_json(
            [
                [_txt("/commands", "primary")],
                [_txt("/status"), _txt("/model")],
                [_txt("/new"), _txt("/stop", "negative")],
            ],
            inline=False,
        )

    def approval_keyboard(self, approval_id: int) -> str:
        return self._keyboard_json(
            [
                [
                    _cb("Allow once", h="ea", id=approval_id, c="once"),
                    _cb("Session", h="ea", id=approval_id, c="session"),
                ],
                [
                    _cb("Always", h="ea", id=approval_id, c="always"),
                    _cb("Deny", h="ea", id=approval_id, c="deny"),
                ],
            ],
            inline=True,
        )

    def slash_confirm_keyboard(self, confirm_id: str) -> str:
        return self._keyboard_json(
            [
                [
                    _cb("Approve once", h="sc", id=confirm_id, c="once"),
                    _cb("Always", h="sc", id=confirm_id, c="always"),
                ],
                [_cb("Cancel", h="sc", id=confirm_id, c="cancel")],
            ],
            inline=True,
        )

    def clarify_keyboard(self, choices: list, clarify_id: str) -> str:
        buttons = [
            _cb(str(index + 1), h="cl", id=clarify_id, c=index)
            for index, _choice in enumerate(choices[:8])
        ]
        buttons.append(_cb("Other", h="cl", id=clarify_id, c="other"))
        return self._keyboard_json(_chunk_buttons(buttons, size=2), inline=True)

    def provider_keyboard(self, providers: list, action_id: str, page: int = 0) -> str:
        page = _bounded_page(page, len(providers), PROVIDER_PAGE_SIZE)
        start = page * PROVIDER_PAGE_SIZE
        visible = providers[start : start + PROVIDER_PAGE_SIZE]
        buttons = [
            _cb(str(offset + 1), h="mpc", i=action_id, pg=page, p=start + offset)
            for offset, _provider in enumerate(visible)
        ]
        rows = _chunk_buttons(buttons, size=3)
        nav = [_cb("Close", h="mc", i=action_id)]
        if page > 0:
            nav.append(_cb("Prev", h="mp", i=action_id, pg=page - 1))
        if start + PROVIDER_PAGE_SIZE < len(providers):
            nav.append(_cb("Next", h="mp", i=action_id, pg=page + 1))
        rows.extend(_chunk_buttons(nav, size=3))
        return self._keyboard_json(rows, inline=True)

    def model_keyboard(
        self,
        provider: dict[str, Any],
        action_id: str,
        provider_index: int,
        page: int = 0,
    ) -> str:
        models = provider.get("models") or []
        page = _bounded_page(page, len(models), MODEL_PAGE_SIZE)
        start = page * MODEL_PAGE_SIZE
        visible = models[start : start + MODEL_PAGE_SIZE]
        buttons = [
            _cb(str(offset + 1), h="mm", i=action_id, p=provider_index, pg=page, m=start + offset)
            for offset, _model_id in enumerate(visible)
        ]
        rows = _chunk_buttons(buttons, size=3)
        nav = [_cb("Back", h="mb", i=action_id)]
        if page > 0:
            nav.append(_cb("Prev", h="mmp", i=action_id, p=provider_index, pg=page - 1))
        if start + MODEL_PAGE_SIZE < len(models):
            nav.append(_cb("Next", h="mmp", i=action_id, p=provider_index, pg=page + 1))
        rows.extend(_chunk_buttons(nav, size=3))
        rows.append([_cb("Close", h="mc", i=action_id)])
        return self._keyboard_json(rows, inline=True)

    def _keyboard_json(self, rows: list[list[dict[str, Any]]], *, inline: bool) -> str:
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
        return json.dumps(
            {"one_time": False, "inline": inline, "buttons": buttons},
            ensure_ascii=False,
            separators=(",", ":"),
        )


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
