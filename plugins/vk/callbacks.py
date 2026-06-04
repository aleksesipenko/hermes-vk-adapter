"""VK callback button routing for Hermes interactive surfaces."""

from __future__ import annotations

import json
import logging
from typing import Any

from .formatting import render_vk_plain_text
from .keyboards import VKKeyboardFactory, model_picker_model_text, model_picker_provider_text
from .utils import _safe_int

logger = logging.getLogger(__name__)


class VKCallbackRouter:
    def __init__(
        self,
        *,
        client: Any,
        is_allowed_user,
        keyboards: VKKeyboardFactory,
        command_keyboard_enabled: bool,
        approval_state: dict[int, str],
        slash_confirm_state: dict[str, str],
        clarify_state: dict[str, str],
        model_picker_state: dict[str, dict[str, Any]],
    ) -> None:
        self.client = client
        self.is_allowed_user = is_allowed_user
        self.keyboards = keyboards
        self.command_keyboard_enabled = command_keyboard_enabled
        self.approval_state = approval_state
        self.slash_confirm_state = slash_confirm_state
        self.clarify_state = clarify_state
        self.model_picker_state = model_picker_state

    async def handle(self, event: dict[str, Any]) -> None:
        payload = decode_payload(event.get("payload"))
        if not payload:
            return
        user_id = _safe_int(event.get("user_id"), 0)
        peer_id = _safe_int(event.get("peer_id"), 0)
        event_id = str(event.get("event_id") or "")
        cmid = _safe_int(event.get("conversation_message_id"), 0)
        if not user_id or not peer_id:
            return
        if not self.is_allowed_user(user_id):
            await self._answer(event_id, user_id, peer_id, "Not authorized")
            return

        kind = str(payload.get("h") or "")
        if kind == "ea":
            await self._approval(payload, event_id, user_id, peer_id, cmid)
        elif kind == "sc":
            await self._slash_confirm(payload, event_id, user_id, peer_id, cmid)
        elif kind == "cl":
            await self._clarify(payload, event_id, user_id, peer_id, cmid)
        elif kind == "mp":
            await self._model_provider_page(payload, event_id, user_id, peer_id, cmid)
        elif kind == "mpc":
            await self._model_provider(payload, event_id, user_id, peer_id, cmid)
        elif kind == "mmp":
            await self._model_model_page(payload, event_id, user_id, peer_id, cmid)
        elif kind == "mb":
            await self._model_back(payload, event_id, user_id, peer_id, cmid)
        elif kind == "mm":
            await self._model_model(payload, event_id, user_id, peer_id, cmid)
        elif kind == "mc":
            await self._model_close(event_id, user_id, peer_id, cmid)

    async def _approval(
        self,
        payload: dict[str, Any],
        event_id: str,
        user_id: int,
        peer_id: int,
        cmid: int,
    ) -> None:
        approval_id = _safe_int(payload.get("id"), 0)
        choice = str(payload.get("c") or "")
        if choice not in {"once", "session", "always", "deny"}:
            await self._answer(event_id, user_id, peer_id, "Invalid approval")
            return
        session_key = self.approval_state.pop(approval_id, None)
        if not session_key:
            await self._answer(event_id, user_id, peer_id, "Already resolved")
            return
        try:
            from tools.approval import resolve_gateway_approval

            resolve_gateway_approval(session_key, choice)
            await self._answer(event_id, user_id, peer_id, f"Resolved: {choice}")
            await self._edit(peer_id, cmid, f"Approval resolved: {choice}")
        except Exception as exc:
            logger.warning("VK approval callback failed: %s", exc)
            await self._answer(event_id, user_id, peer_id, "Approval failed")

    async def _slash_confirm(
        self,
        payload: dict[str, Any],
        event_id: str,
        user_id: int,
        peer_id: int,
        cmid: int,
    ) -> None:
        confirm_id = str(payload.get("id") or "")
        choice = str(payload.get("c") or "")
        if choice not in {"once", "always", "cancel"}:
            await self._answer(event_id, user_id, peer_id, "Invalid choice")
            return
        session_key = self.slash_confirm_state.pop(confirm_id, None)
        if not session_key:
            await self._answer(event_id, user_id, peer_id, "Already resolved")
            return
        try:
            from tools import slash_confirm

            result_text = await slash_confirm.resolve(session_key, confirm_id, choice)
            await self._answer(event_id, user_id, peer_id, f"Resolved: {choice}")
            await self._edit(peer_id, cmid, f"Confirmation resolved: {choice}")
            if result_text:
                await self.client.send_message(
                    peer_id=peer_id,
                    message=render_vk_plain_text(str(result_text)),
                    keyboard=self.keyboards.command_keyboard() if self.command_keyboard_enabled else None,
                )
        except Exception as exc:
            logger.warning("VK slash-confirm callback failed: %s", exc)
            await self._answer(event_id, user_id, peer_id, "Confirmation failed")

    async def _clarify(
        self,
        payload: dict[str, Any],
        event_id: str,
        user_id: int,
        peer_id: int,
        cmid: int,
    ) -> None:
        clarify_id = str(payload.get("id") or "")
        choice = payload.get("c")
        if clarify_id not in self.clarify_state:
            await self._answer(event_id, user_id, peer_id, "Already resolved")
            return
        if choice == "other":
            try:
                from tools.clarify_gateway import mark_awaiting_text

                mark_awaiting_text(clarify_id)
            except Exception as exc:
                logger.warning("VK clarify mark_awaiting_text failed: %s", exc)
            await self._answer(event_id, user_id, peer_id, "Type your answer")
            await self._edit(peer_id, cmid, "Clarify prompt: awaiting typed response")
            return

        idx = _safe_int(choice, -1)
        resolved_text = f"choice {idx + 1}"
        try:
            from tools.clarify_gateway import _entries as clarify_entries  # type: ignore

            entry = clarify_entries.get(clarify_id)
            choices = getattr(entry, "choices", None)
            if choices and 0 <= idx < len(choices):
                resolved_text = str(choices[idx])
        except Exception:
            pass

        self.clarify_state.pop(clarify_id, None)
        try:
            from tools.clarify_gateway import resolve_gateway_clarify

            resolve_gateway_clarify(clarify_id, resolved_text)
            await self._answer(event_id, user_id, peer_id, resolved_text[:90])
            await self._edit(peer_id, cmid, f"Clarify answered: {resolved_text}")
        except Exception as exc:
            logger.warning("VK clarify callback failed: %s", exc)
            await self._answer(event_id, user_id, peer_id, "Clarify failed")

    async def _model_provider_page(
        self,
        payload: dict[str, Any],
        event_id: str,
        user_id: int,
        peer_id: int,
        cmid: int,
    ) -> None:
        state = self.model_picker_state.get(str(peer_id))
        if not state:
            await self._answer(event_id, user_id, peer_id, "Picker expired")
            return
        page = _safe_int(payload.get("pg"), 0)
        state["provider_page"] = page
        providers = state.get("providers") or []
        await self._edit(
            peer_id,
            cmid,
            model_picker_provider_text(
                providers,
                str(state.get("current_model") or ""),
                str(state.get("current_provider") or ""),
                page,
            ),
            keyboard=self.keyboards.provider_keyboard(providers, page),
        )

    async def _model_provider(
        self,
        payload: dict[str, Any],
        event_id: str,
        user_id: int,
        peer_id: int,
        cmid: int,
    ) -> None:
        state = self.model_picker_state.get(str(peer_id))
        if not state:
            await self._answer(event_id, user_id, peer_id, "Picker expired")
            return
        provider_index = _safe_int(payload.get("p"), -1)
        providers = state.get("providers") or []
        if not (0 <= provider_index < len(providers)):
            await self._answer(event_id, user_id, peer_id, "Unknown provider")
            return
        state["provider_page"] = _safe_int(payload.get("pg"), state.get("provider_page", 0))
        state["selected_provider_index"] = provider_index
        state["model_page"] = 0
        provider = providers[provider_index]
        await self._edit(
            peer_id,
            cmid,
            model_picker_model_text(provider, 0),
            keyboard=self.keyboards.model_keyboard(provider, provider_index, 0),
        )

    async def _model_model_page(
        self,
        payload: dict[str, Any],
        event_id: str,
        user_id: int,
        peer_id: int,
        cmid: int,
    ) -> None:
        state = self.model_picker_state.get(str(peer_id))
        if not state:
            await self._answer(event_id, user_id, peer_id, "Picker expired")
            return
        provider_index = _safe_int(payload.get("p"), -1)
        page = _safe_int(payload.get("pg"), 0)
        providers = state.get("providers") or []
        if not (0 <= provider_index < len(providers)):
            await self._answer(event_id, user_id, peer_id, "Unknown provider")
            return
        state["selected_provider_index"] = provider_index
        state["model_page"] = page
        provider = providers[provider_index]
        await self._edit(
            peer_id,
            cmid,
            model_picker_model_text(provider, page),
            keyboard=self.keyboards.model_keyboard(provider, provider_index, page),
        )

    async def _model_back(
        self,
        payload: dict[str, Any],
        event_id: str,
        user_id: int,
        peer_id: int,
        cmid: int,
    ) -> None:
        state = self.model_picker_state.get(str(peer_id))
        if not state:
            await self._answer(event_id, user_id, peer_id, "Picker expired")
            return
        page = _safe_int(payload.get("pg"), state.get("provider_page", 0))
        state["provider_page"] = page
        providers = state.get("providers") or []
        await self._edit(
            peer_id,
            cmid,
            model_picker_provider_text(
                providers,
                str(state.get("current_model") or ""),
                str(state.get("current_provider") or ""),
                page,
            ),
            keyboard=self.keyboards.provider_keyboard(providers, page),
        )

    async def _model_model(
        self,
        payload: dict[str, Any],
        event_id: str,
        user_id: int,
        peer_id: int,
        cmid: int,
    ) -> None:
        state = self.model_picker_state.get(str(peer_id))
        if not state:
            await self._answer(event_id, user_id, peer_id, "Picker expired")
            return
        provider_index = _safe_int(payload.get("p"), -1)
        model_index = _safe_int(payload.get("m"), -1)
        providers = state.get("providers") or []
        if not (0 <= provider_index < len(providers)):
            await self._answer(event_id, user_id, peer_id, "Unknown provider")
            return
        provider = providers[provider_index]
        models = provider.get("models") or []
        if not (0 <= model_index < len(models)):
            await self._answer(event_id, user_id, peer_id, "Unknown model")
            return
        model_id = str(models[model_index])
        provider_slug = str(provider.get("slug") or provider.get("id") or "")
        callback = state.get("on_model_selected")
        try:
            result_text = await callback(str(peer_id), model_id, provider_slug)
            await self._edit(peer_id, cmid, render_vk_plain_text(str(result_text)))
        except Exception as exc:
            logger.warning("VK model picker callback failed: %s", exc)
            await self._answer(event_id, user_id, peer_id, "Model switch failed")

    async def _model_close(self, event_id: str, user_id: int, peer_id: int, cmid: int) -> None:
        self.model_picker_state.pop(str(peer_id), None)
        await self._edit(peer_id, cmid, "Model picker closed.", keyboard=None)

    async def _answer(self, event_id: str, user_id: int, peer_id: int, text: str) -> None:
        if not event_id:
            return
        try:
            await self.client.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                text=text,
            )
        except Exception as exc:
            logger.debug("VK callback answer failed: %s", exc)

    async def _edit(
        self,
        peer_id: int,
        cmid: int,
        message: str,
        keyboard: str | None = None,
    ) -> None:
        if not cmid:
            return
        try:
            await self.client.edit_message(
                peer_id=peer_id,
                cmid=cmid,
                message=message,
                keyboard=keyboard,
            )
        except Exception as exc:
            logger.debug("VK callback edit failed: %s", exc)


def decode_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str) and payload.strip():
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}
