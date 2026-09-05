"""VK callback button routing for Hermes interactive surfaces.

Every branch here follows the same order, and the order is the security
property:

    claim the VK event -> check the allowlist -> authorize against the
    recorded actor -> resolve in Hermes -> only then forget the state

Resolving before authorizing would let any allowlisted user answer another
user's approval prompt.  Forgetting the state before resolving -- which is what
popping the dict up front used to do -- turns a transient resolver failure into
a prompt nobody can ever answer.
"""

from __future__ import annotations

import logging
from typing import Any

from .formatting import render_vk_plain_text
from .interactive import (
    REJECT_EXPIRED,
    REJECT_FOREIGN_PEER,
    REJECT_FOREIGN_USER,
    REJECT_RESOLVED,
    REJECT_UNKNOWN,
    InteractiveStore,
    PendingAction,
)
from .keyboards import VKKeyboardFactory, model_picker_model_text, model_picker_provider_text
from .utils import _safe_int, decode_callback_payload

logger = logging.getLogger(__name__)

#: What the user sees in the VK snackbar. Deliberately vague: the reason codes
#: are for our logs, not for telling a stranger whose prompt they just hit.
_REJECTION_TEXT = {
    REJECT_UNKNOWN: "This action is no longer available",
    REJECT_EXPIRED: "This action expired",
    REJECT_RESOLVED: "Already resolved",
    REJECT_FOREIGN_USER: "This action belongs to someone else",
    REJECT_FOREIGN_PEER: "This action belongs to another chat",
}

APPROVAL_CHOICES = frozenset({"once", "session", "always", "deny"})
SLASH_CONFIRM_CHOICES = frozenset({"once", "always", "cancel"})

#: Store kind for the model picker; its callbacks carry the id as "i".
PICKER_KIND = "mp"


class VKCallbackRouter:
    def __init__(
        self,
        *,
        client: Any,
        is_allowed_user,
        keyboards: VKKeyboardFactory,
        command_keyboard_enabled: bool,
        store: InteractiveStore,
    ) -> None:
        self.client = client
        self.is_allowed_user = is_allowed_user
        self.keyboards = keyboards
        self.command_keyboard_enabled = command_keyboard_enabled
        self.store = store

    async def handle(self, event: dict[str, Any]) -> None:
        payload = decode_callback_payload(event.get("payload"))
        user_id = _safe_int(event.get("user_id"), 0)
        peer_id = _safe_int(event.get("peer_id"), 0)
        event_id = str(event.get("event_id") or "")
        cmid = _safe_int(event.get("conversation_message_id"), 0)
        if not user_id or not peer_id:
            logger.debug("VK callback ignored: missing user_id/peer_id")
            return

        ctx = _CallbackContext(self, event_id, user_id, peer_id, cmid)
        # One VK event gets at most one answer, whatever happens below.
        if not self.store.claim_event(event_id):
            logger.info("VK callback replay suppressed for peer_id=%s", peer_id)
            return
        if not payload:
            await ctx.answer("Unsupported action")
            return
        if not self.is_allowed_user(user_id):
            logger.info("VK callback denied by allowlist: peer_id=%s", peer_id)
            await ctx.answer("Not authorized")
            return

        handler = {
            "ea": self._approval,
            "sc": self._slash_confirm,
            "cl": self._clarify,
            "mp": self._model_provider_page,
            "mpc": self._model_provider,
            "mmp": self._model_model_page,
            "mb": self._model_back,
            "mm": self._model_model,
            "mc": self._model_close,
        }.get(str(payload.get("h") or ""))
        if handler is None:
            logger.debug("VK callback ignored: unknown kind")
            await ctx.answer("Unsupported action")
            return
        await handler(payload, ctx)

    # -- approvals ---------------------------------------------------------

    async def _approval(self, payload: dict[str, Any], ctx: _CallbackContext) -> None:
        choice = str(payload.get("c") or "")
        if choice not in APPROVAL_CHOICES:
            await ctx.answer("Invalid approval")
            return
        action = await ctx.authorize("ea", payload.get("id"))
        if action is None:
            return

        try:
            from tools.approval import resolve_gateway_approval

            resolve_gateway_approval(action.session_key, choice)
        except Exception as exc:
            # State intentionally survives: the button stays usable until it
            # expires instead of the prompt becoming unanswerable.
            logger.warning("VK approval resolution failed: %s", type(exc).__name__)
            await ctx.answer("Approval failed, try again")
            return

        self.store.discard("ea", action.action_id)
        await ctx.answer(f"Resolved: {choice}")
        await ctx.edit(f"Approval resolved: {choice}")

    async def _slash_confirm(self, payload: dict[str, Any], ctx: _CallbackContext) -> None:
        choice = str(payload.get("c") or "")
        if choice not in SLASH_CONFIRM_CHOICES:
            await ctx.answer("Invalid choice")
            return
        action = await ctx.authorize("sc", payload.get("id"))
        if action is None:
            return

        try:
            from tools import slash_confirm

            result_text = await slash_confirm.resolve(action.session_key, action.action_id, choice)
        except Exception as exc:
            logger.warning("VK slash-confirm resolution failed: %s", type(exc).__name__)
            await ctx.answer("Confirmation failed, try again")
            return

        self.store.discard("sc", action.action_id)
        await ctx.answer(f"Resolved: {choice}")
        await ctx.edit(f"Confirmation resolved: {choice}")
        if result_text:
            await self.client.send_message(
                peer_id=ctx.peer_id,
                message=render_vk_plain_text(str(result_text)),
                keyboard=(
                    self.keyboards.command_keyboard() if self.command_keyboard_enabled else None
                ),
            )

    async def _clarify(self, payload: dict[str, Any], ctx: _CallbackContext) -> None:
        action = await ctx.authorize("cl", payload.get("id"))
        if action is None:
            return
        choice = payload.get("c")

        if choice == "other":
            try:
                from tools.clarify_gateway import mark_awaiting_text

                mark_awaiting_text(action.action_id)
            except Exception as exc:
                logger.warning("VK clarify mark_awaiting_text failed: %s", type(exc).__name__)
            # Not a terminal resolution: the typed answer still has to arrive.
            await ctx.answer("Type your answer")
            await ctx.edit("Clarify prompt: awaiting typed response")
            return

        # Choices are recorded when the prompt is sent, so resolving one never
        # requires reaching into Hermes' private clarify state.
        choices = action.data.get("choices") or []
        index = _safe_int(choice, -1)
        if not 0 <= index < len(choices):
            await ctx.answer("Unknown choice")
            return
        resolved_text = str(choices[index])

        try:
            from tools.clarify_gateway import resolve_gateway_clarify

            resolve_gateway_clarify(action.action_id, resolved_text)
        except Exception as exc:
            logger.warning("VK clarify resolution failed: %s", type(exc).__name__)
            await ctx.answer("Clarify failed, try again")
            return

        self.store.discard("cl", action.action_id)
        await ctx.answer(resolved_text[:90])
        await ctx.edit(f"Clarify answered: {resolved_text}")

    # -- model picker ------------------------------------------------------

    async def _picker(self, payload: dict[str, Any], ctx: _CallbackContext):
        return await ctx.authorize(PICKER_KIND, payload.get("i"), rejection="Picker expired")

    async def _model_provider_page(self, payload: dict[str, Any], ctx: _CallbackContext) -> None:
        action = await self._picker(payload, ctx)
        if action is None:
            return
        page = _safe_int(payload.get("pg"), 0)
        action.data["provider_page"] = page
        await self._render_providers(action, ctx, page)

    async def _model_back(self, payload: dict[str, Any], ctx: _CallbackContext) -> None:
        action = await self._picker(payload, ctx)
        if action is None:
            return
        page = _safe_int(payload.get("pg"), action.data.get("provider_page", 0))
        action.data["provider_page"] = page
        await self._render_providers(action, ctx, page)

    async def _model_provider(self, payload: dict[str, Any], ctx: _CallbackContext) -> None:
        action = await self._picker(payload, ctx)
        if action is None:
            return
        providers = action.data.get("providers") or []
        provider_index = _safe_int(payload.get("p"), -1)
        if not 0 <= provider_index < len(providers):
            await ctx.answer("Unknown provider")
            return
        action.data["provider_page"] = _safe_int(
            payload.get("pg"), action.data.get("provider_page", 0)
        )
        action.data["selected_provider_index"] = provider_index
        action.data["model_page"] = 0
        await self._render_models(action, ctx, providers[provider_index], provider_index, 0)

    async def _model_model_page(self, payload: dict[str, Any], ctx: _CallbackContext) -> None:
        action = await self._picker(payload, ctx)
        if action is None:
            return
        providers = action.data.get("providers") or []
        provider_index = _safe_int(payload.get("p"), -1)
        if not 0 <= provider_index < len(providers):
            await ctx.answer("Unknown provider")
            return
        page = _safe_int(payload.get("pg"), 0)
        action.data["selected_provider_index"] = provider_index
        action.data["model_page"] = page
        await self._render_models(action, ctx, providers[provider_index], provider_index, page)

    async def _model_model(self, payload: dict[str, Any], ctx: _CallbackContext) -> None:
        action = await self._picker(payload, ctx)
        if action is None:
            return
        providers = action.data.get("providers") or []
        provider_index = _safe_int(payload.get("p"), -1)
        if not 0 <= provider_index < len(providers):
            await ctx.answer("Unknown provider")
            return
        provider = providers[provider_index]
        models = provider.get("models") or []
        model_index = _safe_int(payload.get("m"), -1)
        if not 0 <= model_index < len(models):
            await ctx.answer("Unknown model")
            return

        model_id = str(models[model_index])
        provider_slug = str(provider.get("slug") or provider.get("id") or "")
        callback = action.data.get("on_model_selected")
        try:
            result_text = await callback(str(ctx.peer_id), model_id, provider_slug)
        except Exception as exc:
            logger.warning("VK model switch failed: %s", type(exc).__name__)
            await ctx.answer("Model switch failed")
            return

        self.store.discard(PICKER_KIND, action.action_id)
        await ctx.edit(render_vk_plain_text(str(result_text)))

    async def _model_close(self, payload: dict[str, Any], ctx: _CallbackContext) -> None:
        action = await self._picker(payload, ctx)
        if action is None:
            return
        self.store.discard(PICKER_KIND, action.action_id)
        await ctx.edit("Model picker closed.", keyboard=None)

    async def _render_providers(
        self, action: PendingAction, ctx: _CallbackContext, page: int
    ) -> None:
        providers = action.data.get("providers") or []
        await ctx.edit(
            model_picker_provider_text(
                providers,
                str(action.data.get("current_model") or ""),
                str(action.data.get("current_provider") or ""),
                page,
            ),
            keyboard=self.keyboards.provider_keyboard(providers, action.action_id, page),
        )

    async def _render_models(
        self,
        action: PendingAction,
        ctx: _CallbackContext,
        provider: dict[str, Any],
        provider_index: int,
        page: int,
    ) -> None:
        await ctx.edit(
            model_picker_model_text(provider, page),
            keyboard=self.keyboards.model_keyboard(
                provider, action.action_id, provider_index, page
            ),
        )


class _CallbackContext:
    """One inbound VK callback: answered at most once, edits its own message."""

    def __init__(
        self,
        router: VKCallbackRouter,
        event_id: str,
        user_id: int,
        peer_id: int,
        cmid: int,
    ) -> None:
        self._router = router
        self.event_id = event_id
        self.user_id = user_id
        self.peer_id = peer_id
        self.cmid = cmid
        self._answered = False

    async def authorize(
        self, kind: str, action_id: Any, *, rejection: str | None = None
    ) -> PendingAction | None:
        action, reason = self._router.store.authorize(
            kind=kind, action_id=action_id, user_id=self.user_id, peer_id=self.peer_id
        )
        if action is not None:
            return action
        # Reason codes only: never log the session key or the prompt body.
        logger.info(
            "VK callback rejected: kind=%s reason=%s peer_id=%s", kind, reason, self.peer_id
        )
        await self.answer(rejection or _REJECTION_TEXT.get(reason, "Unavailable"))
        return None

    async def answer(self, text: str) -> None:
        if self._answered or not self.event_id:
            return
        self._answered = True
        try:
            await self._router.client.send_message_event_answer(
                event_id=self.event_id,
                user_id=self.user_id,
                peer_id=self.peer_id,
                text=text,
            )
        except Exception as exc:
            logger.debug("VK callback answer failed: %s", type(exc).__name__)

    async def edit(self, message: str, keyboard: str | None = None) -> None:
        if not self.cmid:
            return
        try:
            await self._router.client.edit_message(
                peer_id=self.peer_id,
                cmid=self.cmid,
                message=message,
                keyboard=keyboard,
            )
        except Exception as exc:
            logger.debug("VK callback edit failed: %s", type(exc).__name__)
