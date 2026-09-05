"""Regressions for the hard-review findings against eab6418..6db6b19.

Each test names the defect it locks down, so a future change that reintroduces
it fails here rather than in production.
"""

from __future__ import annotations

import asyncio
import re

import httpx
import pytest

from plugins.vk.adapter import VKAdapter, _describe_payload
from plugins.vk.attachments import ContextLimits, summarize_message_context
from plugins.vk.interactive import InteractiveStore
from plugins.vk.state import BoundedTTLCache
from plugins.vk.utils import (
    MAX_INBOUND_ATTACHMENTS,
    VK_MESSAGE_EDIT_LIMIT,
    ReactionConfig,
)

pytestmark = pytest.mark.hermes_contract

DM_PEER = 987654321
GROUP_PEER = 2_000_000_001
USER_A = 101
USER_B = 202


class RecordingClient:
    def __init__(self, *, fail_send=None):
        self.sends = []
        self.edits = []
        self.reactions = []
        self._n = 0
        self._fail_send = fail_send

    def new_random_id(self):
        self._n += 1
        return 1000 + self._n

    async def send_message(self, **kwargs):
        if self._fail_send:
            raise self._fail_send
        self.sends.append(kwargs)
        return 900 + len(self.sends)

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)
        return 1

    async def send_reaction(self, **kwargs):
        self.reactions.append(kwargs)
        return 1

    async def delete_reaction(self, **kwargs):
        return 1

    async def mark_as_read(self, **kwargs):
        return 1


def make_adapter(client=None, **overrides):
    adapter = object.__new__(VKAdapter)
    adapter.client = client or RecordingClient()
    adapter.group_id = 123456789
    adapter.max_message_length = 9000
    adapter.command_keyboard_enabled = False
    adapter.allow_all_users = True
    adapter.allowed_users = set()
    adapter.require_mention = False
    adapter.mark_read_enabled = False
    adapter.reactions = ReactionConfig()
    adapter._approval_counter = 0
    adapter._interactive = InteractiveStore()
    adapter._outbound_random_ids = BoundedTTLCache(max_entries=64, ttl_seconds=120)
    adapter._cmid_by_anchor = BoundedTTLCache(max_entries=16, ttl_seconds=600)
    adapter._capabilities = BoundedTTLCache(max_entries=16, ttl_seconds=600)
    adapter._identities = BoundedTTLCache(max_entries=16, ttl_seconds=600)
    from plugins.vk.keyboards import VKKeyboardFactory

    adapter._keyboards = VKKeyboardFactory()
    for key, value in overrides.items():
        setattr(adapter, key, value)
    return adapter


MODERN = {"button_actions": ["text", "callback"], "keyboard": True, "inline_keyboard": True}


def build_session_key(*args, **kwargs):
    from gateway.session import build_session_key as _build

    return _build(*args, **kwargs)


def _vk_group_source():
    """A SessionSource for a VK group chat, using core's own types.

    Registering the plugin first is what teaches Hermes' Platform enum about
    "vk" -- the same precondition cron and the gateway establish.
    """
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    from plugins.vk.adapter import register

    register(PluginContext(PluginManifest(name="vk"), PluginManager()))

    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(
        platform=Platform("vk"),
        chat_id=str(GROUP_PEER),
        chat_type="group",
        user_id=str(USER_A),
    )


# ── Finding 1: "/approve deny" APPROVES in current Hermes ─────────────────


def test_the_denial_instruction_uses_a_command_that_actually_denies():
    """Contract test against the real core handler, not our own wording.

    gateway/slash_commands.py::_handle_approve_command parses only "all",
    "session" and "always"; every other argument falls through to
    choice = "once". So "/approve deny" AUTHORIZES the command.
    """
    import inspect

    from gateway.slash_commands import GatewaySlashCommandsMixin

    source = inspect.getsource(GatewaySlashCommandsMixin._handle_approve_command)
    recognised = set(re.findall(r'a in \{([^}]*)\}', source))
    tokens = {tok.strip().strip('"\'') for group in recognised for tok in group.split(",")}
    assert "deny" not in tokens, "core /approve now understands deny; revisit this fallback"

    # A real /deny handler must exist for the fallback to point at.
    assert hasattr(GatewaySlashCommandsMixin, "_handle_deny_command")

    text = VKAdapter._approval_fallback_text(make_adapter())
    assert "/deny" in text
    assert "/approve deny" not in text


# ── Finding 2: client_info sits beside object.message ─────────────────────


@pytest.mark.asyncio
async def test_capabilities_are_read_from_the_real_event_shape():
    """Official message_new: object.client_info, sibling of object.message."""
    adapter = make_adapter()
    captured = []

    async def fake_handle_message(event):
        captured.append(event)

    async def no_media(_message):
        from gateway.platforms.base import MessageType

        return [], [], MessageType.TEXT, []

    adapter.handle_message = fake_handle_message
    adapter._extract_media = no_media
    adapter.build_source = lambda **kw: None

    await VKAdapter._handle_update(
        adapter,
        {
            "type": "message_new",
            "object": {
                "message": {"peer_id": DM_PEER, "from_id": DM_PEER, "id": 7, "text": "hi"},
                "client_info": MODERN,
            },
        },
    )

    assert len(captured) == 1
    assert VKAdapter._callbacks_supported(adapter, DM_PEER) is True


@pytest.mark.asyncio
async def test_every_interactive_surface_degrades_without_callback_support():
    """slash_confirm and the model picker used to send buttons regardless."""
    adapter = make_adapter()  # no capabilities recorded -> unsupported

    await VKAdapter.send_exec_approval(
        adapter, chat_id=str(DM_PEER), command="rm -rf /", session_key=f"agent:main:vk:dm:{DM_PEER}"
    )
    await VKAdapter.send_slash_confirm(
        adapter,
        chat_id=str(DM_PEER),
        title="t",
        message="confirm?",
        session_key=f"agent:main:vk:dm:{DM_PEER}",
        confirm_id="c1",
    )
    picker = await VKAdapter.send_model_picker(
        adapter,
        chat_id=str(DM_PEER),
        providers=[{"slug": "p", "name": "P", "models": ["m"]}],
        current_model="m",
        current_provider="p",
        session_key=f"agent:main:vk:dm:{DM_PEER}",
        on_model_selected=None,
    )

    # Approval and slash-confirm degrade in place; the picker yields to core.
    assert [call.get("keyboard") for call in adapter.client.sends] == [None, None]
    for call in adapter.client.sends:
        assert "/approve" in call["message"]
    assert not picker.success


@pytest.mark.asyncio
async def test_a_capable_client_still_gets_buttons_on_every_surface():
    adapter = make_adapter()
    VKAdapter._remember_capabilities(adapter, DM_PEER, MODERN)
    session = f"agent:main:vk:dm:{DM_PEER}"

    await VKAdapter.send_exec_approval(
        adapter, chat_id=str(DM_PEER), command="ls", session_key=session
    )
    await VKAdapter.send_slash_confirm(
        adapter, chat_id=str(DM_PEER), title="t", message="m", session_key=session, confirm_id="c1"
    )

    assert all(call.get("keyboard") for call in adapter.client.sends)


# ── Finding 3: actor binding must not race ───────────────────────────────


def test_the_actor_is_derived_from_the_real_hermes_session_key():
    """Built with core's own build_session_key, not a hand-written string."""
    source = _vk_group_source()
    key = build_session_key(source, group_sessions_per_user=True)

    adapter = make_adapter()
    assert VKAdapter._provable_actor(adapter, GROUP_PEER, key) == USER_A


def test_a_shared_group_session_yields_no_provable_actor():
    shared = build_session_key(_vk_group_source(), group_sessions_per_user=False)

    adapter = make_adapter()
    assert VKAdapter._provable_actor(adapter, GROUP_PEER, shared) is None


def test_a_dm_actor_is_the_peer_itself():
    adapter = make_adapter()

    assert VKAdapter._provable_actor(adapter, DM_PEER, "anything") == DM_PEER


@pytest.mark.asyncio
async def test_a_concurrent_second_speaker_cannot_steal_the_first_users_approval():
    """The race the old "last actor in peer" cache lost.

    Base handle_message() schedules processing and returns, so user B can be
    fully handled between user A's turn and A's approval hook firing.
    """
    adapter = make_adapter()
    VKAdapter._remember_capabilities(adapter, GROUP_PEER, MODERN)
    session_a = f"agent:main:vk:group:{GROUP_PEER}:{USER_A}"
    session_b = f"agent:main:vk:group:{GROUP_PEER}:{USER_B}"

    # Both users' approvals are raised concurrently, in the "wrong" order.
    await asyncio.gather(
        VKAdapter.send_exec_approval(
            adapter, chat_id=str(GROUP_PEER), command="a", session_key=session_a
        ),
        VKAdapter.send_exec_approval(
            adapter, chat_id=str(GROUP_PEER), command="b", session_key=session_b
        ),
    )

    bound = {}
    for action_id in ("1", "2"):
        for user in (USER_A, USER_B):
            action, _ = adapter._interactive.authorize(
                kind="ea", action_id=action_id, user_id=user, peer_id=GROUP_PEER
            )
            if action:
                bound[action_id] = user
    assert set(bound.values()) == {USER_A, USER_B}, bound


@pytest.mark.asyncio
async def test_an_unprovable_group_actor_gets_no_callbacks_and_no_state():
    adapter = make_adapter()
    VKAdapter._remember_capabilities(adapter, GROUP_PEER, MODERN)

    await VKAdapter.send_exec_approval(
        adapter,
        chat_id=str(GROUP_PEER),
        command="rm -rf /",
        session_key=f"agent:main:vk:group:{GROUP_PEER}",  # shared, no user segment
    )

    assert adapter.client.sends[0]["keyboard"] is None
    assert "/deny" in adapter.client.sends[0]["message"]
    for user in (USER_A, USER_B, 0):
        action, _ = adapter._interactive.authorize(
            kind="ea", action_id="1", user_id=user, peer_id=GROUP_PEER
        )
        assert action is None


def test_the_mutable_last_actor_cache_is_gone():
    import inspect

    from plugins.vk import adapter as adapter_module

    source = inspect.getsource(adapter_module)
    assert "_last_actor " not in source
    assert "_actor_for_peer" not in source


# ── Finding 4: distinct sends must not collide ───────────────────────────


@pytest.mark.asyncio
async def test_two_distinct_sends_of_identical_text_get_distinct_random_ids():
    """VK dedupes by random_id, so a shared id silently drops the second."""
    adapter = make_adapter()

    await VKAdapter.send(adapter, str(DM_PEER), "same text")
    await VKAdapter.send(adapter, str(DM_PEER), "same text")

    ids = [call["random_id"] for call in adapter.client.sends]
    assert len(ids) == 2
    assert ids[0] != ids[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ReadTimeout("t"), httpx.ConnectError("c")])
async def test_transport_retries_reuse_the_same_random_id(failure):
    """Retry stability comes from re-issuing identical params, not caching."""
    import respx
    from httpx import Response

    from plugins.vk.client import VKRestClient

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    client = VKRestClient("t" * 85, 1, "5.199", sleep=fake_sleep)
    seen = []

    def responder(request):
        seen.append(request.content)
        if len(seen) < 3:
            raise type(failure)(str(failure))
        return Response(200, json={"response": 5})

    with respx.mock:
        respx.post("https://api.vk.com/method/messages.send").mock(side_effect=responder)
        try:
            await client.send_message(peer_id=1, message="hi", random_id=777)
        finally:
            await client.close()

    assert len(seen) == 3
    assert all(b"random_id=777" in body for body in seen)


# ── Finding 5: never arm core before delivery succeeds ───────────────────


@pytest.mark.asyncio
async def test_a_failed_clarify_send_never_arms_text_capture(monkeypatch):
    marks = []
    monkeypatch.setattr(
        "tools.clarify_gateway.mark_awaiting_text", lambda cid: marks.append(cid) or True
    )
    adapter = make_adapter(RecordingClient(fail_send=httpx.ConnectError("down")))

    result = await VKAdapter.send_clarify(
        adapter,
        chat_id=str(DM_PEER),
        question="q",
        choices=["a", "b"],
        clarify_id="cl1",
        session_key=f"agent:main:vk:dm:{DM_PEER}",
    )

    assert not result.success
    assert marks == [], "core was armed for a prompt the user never received"


@pytest.mark.asyncio
async def test_a_delivered_fallback_that_cannot_arm_core_is_reported_as_failed(monkeypatch):
    monkeypatch.setattr("tools.clarify_gateway.mark_awaiting_text", lambda cid: False)
    adapter = make_adapter()

    result = await VKAdapter.send_clarify(
        adapter,
        chat_id=str(DM_PEER),
        question="q",
        choices=["a", "b"],
        clarify_id="cl1",
        session_key=f"agent:main:vk:dm:{DM_PEER}",
    )

    assert not result.success
    assert "armed" in (result.error or "")


@pytest.mark.asyncio
async def test_a_successful_fallback_arms_core_after_delivery(monkeypatch):
    order = []
    monkeypatch.setattr(
        "tools.clarify_gateway.mark_awaiting_text", lambda cid: order.append("mark") or True
    )
    adapter = make_adapter()

    class OrderedClient(RecordingClient):
        async def send_message(self, **kwargs):
            order.append("send")
            return await super().send_message(**kwargs)

    adapter.client = OrderedClient()

    result = await VKAdapter.send_clarify(
        adapter,
        chat_id=str(DM_PEER),
        question="q",
        choices=["a"],
        clarify_id="cl1",
        session_key=f"agent:main:vk:dm:{DM_PEER}",
    )

    assert result.success
    assert order == ["send", "mark"]


# ── Finding 6: budgets must be totals ────────────────────────────────────


def test_attachment_and_text_budgets_are_totals_across_the_whole_walk():
    limits = ContextLimits(max_depth=2, max_messages=5, max_text_chars=120, max_attachments=2)
    forwards = [
        {
            "from_id": index,
            "text": "t",
            "attachments": [{"type": "doc", "doc": {"title": f"d{n}.pdf"}} for n in range(5)],
        }
        for index in range(3)
    ]

    context = summarize_message_context({"fwd_messages": forwards}, limits)

    assert context.count("[document") <= limits.max_attachments
    assert len(context) <= limits.max_text_chars + 200
    assert "truncated" in context.lower()


@pytest.mark.asyncio
async def test_inbound_attachment_processing_is_capped(monkeypatch):
    downloads = []

    class CountingClient(RecordingClient):
        async def download_bytes(self, url, *, max_bytes=None):
            downloads.append(url)
            return b"x"

    monkeypatch.setattr("plugins.vk.adapter.cache_image_from_bytes", lambda data, ext: "/tmp/x.jpg")
    adapter = make_adapter(CountingClient())

    attachments = [
        {"type": "photo", "photo": {"sizes": [{"width": 1, "height": 1, "url": f"u{i}"}]}}
        for i in range(40)
    ]
    _paths, _types, _kind, notes = await VKAdapter._extract_media(
        adapter, {"attachments": attachments}
    )

    assert len(downloads) <= MAX_INBOUND_ATTACHMENTS
    assert any("not processed" in note for note in notes)


# ── Finding 7: doctor must send a real group_id ──────────────────────────


@pytest.mark.asyncio
async def test_doctor_sends_the_real_group_id_on_community_methods():
    from plugins.vk.doctor import REQUIRED_LONGPOLL_EVENTS, REQUIRED_SCOPES, run_live_checks

    class Client:
        group_id = 123456789

        def __init__(self):
            self.calls = []

        async def call(self, method, **params):
            self.calls.append((method, params))
            return {
                "groups.getTokenPermissions": {
                    "settings": [{"setting": 1, "name": n} for n in REQUIRED_SCOPES]
                },
                "groups.getLongPollSettings": {
                    "is_enabled": True,
                    "api_version": "5.199",
                    "events": dict.fromkeys(REQUIRED_LONGPOLL_EVENTS, 1),
                },
                "groups.getLongPollServer": {"server": "s", "key": "k", "ts": "1"},
            }[method]

    client = Client()
    await run_live_checks(client, api_version="5.199")

    params = dict(client.calls)
    assert params["groups.getLongPollSettings"] == {"group_id": 123456789}
    assert params["groups.getLongPollServer"] == {"group_id": 123456789}


@pytest.mark.asyncio
async def test_doctor_refuses_to_probe_without_a_positive_group_id():
    from plugins.vk.doctor import run_live_checks

    class Client:
        group_id = 0

        async def call(self, method, **params):  # pragma: no cover - must not run
            raise AssertionError("probed without a group id")

    results = await run_live_checks(Client())

    assert [r.status for r in results] == ["fail"]


# ── Finding 8: streaming edits must not duplicate ────────────────────────


@pytest.mark.asyncio
async def test_streamed_overflow_edits_deliver_every_character_exactly_once():
    """Mirrors gateway/stream_consumer.py: repeated full-text edits, then finalize."""
    adapter = make_adapter()
    growing = ["word " * 1200, "word " * 1800, "word " * 2400]

    for partial in growing:
        result = await VKAdapter.edit_message(
            adapter, str(DM_PEER), "555", partial, finalize=False
        )
        assert result.success
        # A non-final edit must never create a continuation.
        assert result.continuation_message_ids == ()
        assert result.message_id == "555"
    assert adapter.client.sends == [], "non-final edits posted continuations"

    final = growing[-1]
    result = await VKAdapter.edit_message(adapter, str(DM_PEER), "555", final, finalize=True)

    assert result.success
    visible = [adapter.client.edits[-1]["message"], *[c["message"] for c in adapter.client.sends]]
    assert all(len(part) <= VK_MESSAGE_EDIT_LIMIT for part in visible)
    assert re.sub(r"\s+", "", "".join(visible)) == re.sub(r"\s+", "", final)

    ids = ["555", *[str(900 + n) for n in range(1, len(adapter.client.sends) + 1)]]
    assert result.message_id == ids[-1]
    assert list(result.continuation_message_ids) == ids[:-1]


@pytest.mark.asyncio
async def test_a_short_non_final_edit_still_updates_in_place():
    adapter = make_adapter()

    result = await VKAdapter.edit_message(adapter, str(DM_PEER), "555", "short", finalize=False)

    assert result.success
    assert adapter.client.edits[-1]["message"] == "short"
    assert adapter.client.sends == []


# ── Finding 9: privacy, health, auth ─────────────────────────────────────


def test_debug_logging_describes_the_payload_without_its_contents():
    payload = {
        "ts": "42",
        "updates": [
            {
                "type": "message_new",
                "object": {"message": {"text": "my private message", "peer_id": 1}},
            },
            {"type": "message_event", "object": {"payload": {"h": "ea", "id": 1}}},
        ],
    }

    described = _describe_payload(payload)

    assert "my private message" not in described
    assert '"h"' not in described
    assert "message_new" in described and "updates=2" in described


@pytest.mark.asyncio
async def test_a_failed_send_raises_the_configured_failure_reaction():
    adapter = make_adapter(
        RecordingClient(fail_send=httpx.ConnectError("down")),
        reactions=ReactionConfig(failed=5),
    )
    adapter._cmid_by_anchor.set((DM_PEER, "7"), 3)

    result = await VKAdapter.send(adapter, str(DM_PEER), "text", reply_to="7")

    assert not result.success
    assert adapter.client.reactions == [{"peer_id": DM_PEER, "cmid": 3, "reaction_id": 5}]


@pytest.mark.parametrize(
    ("allow_all", "allowlist", "expected"),
    [
        (True, set(), True),
        (False, {"101"}, True),
        (False, {"999"}, False),
        (False, set(), False),  # unconfigured must NOT mean allow
    ],
)
def test_inbound_side_effects_require_explicit_authorization(allow_all, allowlist, expected):
    adapter = make_adapter(allow_all_users=allow_all, allowed_users=allowlist)

    assert VKAdapter._is_allowed_vk_user(adapter, 101) is expected


# ══ second review pass ════════════════════════════════════════════════════


# ── Finding 1b: the total download budget must be hard ───────────────────


@pytest.mark.asyncio
async def test_the_total_download_budget_is_enforced_during_the_stream(monkeypatch):
    """Measuring after the fact let a big body overshoot the remaining cap."""
    from plugins.vk.utils import MAX_INBOUND_DOWNLOAD_BYTES

    fetched = []
    cached = []

    class BudgetedClient(RecordingClient):
        async def download_bytes(self, url, *, max_bytes=None):
            assert max_bytes is not None and max_bytes > 0
            # A hostile server would return more than asked; the real client
            # aborts mid-stream, so the most a caller can ever receive is the
            # budget it passed.
            served = min(20 * 1024 * 1024, max_bytes)
            fetched.append(served)
            return b"\xff\xd8\xff" + b"0" * (served - 3)

    monkeypatch.setattr(
        "plugins.vk.adapter.cache_image_from_bytes",
        lambda data, ext: cached.append(len(data)) or "/tmp/x.jpg",
    )
    adapter = make_adapter(BudgetedClient())

    attachments = [
        {"type": "photo", "photo": {"sizes": [{"width": 1, "height": 1, "url": f"u{i}"}]}}
        for i in range(8)
    ]
    _paths, _types, _kind, notes = await VKAdapter._extract_media(
        adapter, {"attachments": attachments}
    )

    assert sum(fetched) <= MAX_INBOUND_DOWNLOAD_BYTES
    assert sum(cached) <= MAX_INBOUND_DOWNLOAD_BYTES
    assert any("budget exhausted" in note for note in notes)


@pytest.mark.asyncio
async def test_audio_downloads_are_budgeted_too(monkeypatch):
    seen = []

    class BudgetedClient(RecordingClient):
        async def download_bytes(self, url, *, max_bytes=None):
            seen.append(max_bytes)
            return b"OggS"

    monkeypatch.setattr("plugins.vk.adapter.cache_audio_from_bytes", lambda data, ext: "/tmp/a.ogg")
    adapter = make_adapter(BudgetedClient())

    await VKAdapter._extract_media(
        adapter,
        {"attachments": [{"type": "audio_message", "audio_message": {"link_ogg": "https://vk/x"}}]},
    )

    assert seen and all(value and value > 0 for value in seen)


@pytest.mark.asyncio
async def test_a_download_budget_of_zero_is_refused_by_the_client():
    from plugins.vk.client import VKRestClient

    client = VKRestClient("t" * 85, 1, "5.199")
    try:
        with pytest.raises(ValueError, match="budget is exhausted"):
            await client.download_bytes("https://vk.example/x", max_bytes=0)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_attachment_urls_are_checked_before_download():
    """VK-supplied URLs are still untrusted input."""
    from plugins.vk.client import VKRestClient

    client = VKRestClient("t" * 85, 1, "5.199")
    try:
        with pytest.raises(ValueError, match="http"):
            await client.download_bytes("file:///etc/passwd")
    finally:
        await client.close()


# ── Finding 2: max_text_chars is an exact total ──────────────────────────


@pytest.mark.parametrize("limit", [20, 60, 120, 400])
def test_rendered_context_never_exceeds_max_text_chars(limit):
    """Labels and the truncation marker are output too, so they count."""
    limits = ContextLimits(
        max_depth=3, max_messages=10, max_text_chars=limit, max_attachments=5
    )
    forwards = [
        {
            "from_id": index,
            "text": "some forwarded body text that is not short",
            "attachments": [{"type": "doc", "doc": {"title": f"d{n}.pdf"}} for n in range(4)],
        }
        for index in range(5)
    ]

    context = summarize_message_context({"fwd_messages": forwards}, limits)

    assert len(context) <= limit


def test_truncation_stays_truthful_even_at_a_tiny_budget():
    limits = ContextLimits(max_depth=2, max_messages=5, max_text_chars=25, max_attachments=2)

    context = summarize_message_context(
        {"fwd_messages": [{"from_id": 1, "text": "x" * 500}]}, limits
    )

    assert len(context) <= 25
    assert "truncated" in context.lower()


# ── Finding 3: reactions must target the message they answer ─────────────


@pytest.mark.asyncio
async def test_a_second_speaker_does_not_steal_the_first_users_reaction():
    """A's reply must react to A's message even after B's arrived."""
    adapter = make_adapter(reactions=ReactionConfig(done=16))
    adapter._cmid_by_anchor.set((GROUP_PEER, "a-anchor"), 11)
    adapter._cmid_by_anchor.set((GROUP_PEER, "b-anchor"), 22)

    await VKAdapter.send(adapter, str(GROUP_PEER), "answer to A", reply_to="a-anchor")

    assert adapter.client.reactions == [
        {"peer_id": GROUP_PEER, "cmid": 11, "reaction_id": 16}
    ]


@pytest.mark.asyncio
async def test_a_proactive_send_reacts_to_nothing():
    """Cron delivery answers no message, so there is no target to react to."""
    adapter = make_adapter(reactions=ReactionConfig(done=16))
    adapter._cmid_by_anchor.set((GROUP_PEER, "stale"), 11)

    await VKAdapter.send(adapter, str(GROUP_PEER), "scheduled report")

    assert adapter.client.reactions == []


@pytest.mark.asyncio
async def test_an_unknown_anchor_reacts_to_nothing():
    adapter = make_adapter(reactions=ReactionConfig(done=16))

    await VKAdapter.send(adapter, str(GROUP_PEER), "answer", reply_to="never-seen")

    assert adapter.client.reactions == []


@pytest.mark.asyncio
async def test_media_delivery_uses_the_same_reaction_rule(tmp_path):
    adapter = make_adapter(reactions=ReactionConfig(done=16))
    adapter._cmid_by_anchor.set((GROUP_PEER, "a-anchor"), 11)

    class DocClient(RecordingClient):
        async def upload_document_raw(self, *, peer_id, path, title=None):
            return "doc-1_1"

    adapter.client = DocClient()
    adapter._cmid_by_anchor.set((GROUP_PEER, "a-anchor"), 11)
    doc = tmp_path / "r.pdf"
    doc.write_bytes(b"pdf")

    await VKAdapter.send_media_files(
        adapter, str(GROUP_PEER), [str(doc)], "caption", reply_to="a-anchor"
    )

    assert adapter.client.reactions == [
        {"peer_id": GROUP_PEER, "cmid": 11, "reaction_id": 16}
    ]


@pytest.mark.asyncio
async def test_the_inbound_handler_records_the_anchor_it_puts_on_the_event():
    adapter = make_adapter()
    captured = []

    async def fake_handle_message(event):
        captured.append(event)

    async def no_media(_message):
        from gateway.platforms.base import MessageType

        return [], [], MessageType.TEXT, []

    adapter.handle_message = fake_handle_message
    adapter._extract_media = no_media
    adapter.build_source = lambda **kw: None

    await VKAdapter._handle_message_new(
        adapter,
        {"peer_id": GROUP_PEER, "from_id": USER_A, "text": "hi", "conversation_message_id": 77},
        {"type": "message_new"},
    )

    anchor = captured[0].message_id
    assert VKAdapter._trigger_cmid(adapter, GROUP_PEER, anchor) == 77


# ── Finding 4: local doctor rejects an invalid configured home peer ──────


@pytest.mark.parametrize("value", ["abc", "-1", "0", "2000000001.5", "club2000000001"])
def test_a_configured_but_unusable_home_peer_fails_locally(value):
    """A whitespace-only value strips to empty and is treated as absent."""
    from plugins.vk.doctor import run_local_checks

    env = {
        "VK_GROUP_TOKEN": "a" * 85,
        "VK_GROUP_ID": "123456789",
        "VK_HOME_PEER_ID": value,
    }
    statuses = {r.name: r.status for r in run_local_checks(env)}

    assert statuses["home_peer_id"] == "fail"


def test_an_absent_home_peer_is_still_only_skipped():
    from plugins.vk.doctor import run_local_checks

    env = {"VK_GROUP_TOKEN": "a" * 85, "VK_GROUP_ID": "123456789"}
    statuses = {r.name: r.status for r in run_local_checks(env)}

    assert statuses["home_peer_id"] == "skip"


def test_a_valid_home_peer_passes_locally():
    from plugins.vk.doctor import run_local_checks

    env = {
        "VK_GROUP_TOKEN": "a" * 85,
        "VK_GROUP_ID": "123456789",
        "VK_HOME_PEER_ID": "2000000001",
    }
    statuses = {r.name: r.status for r in run_local_checks(env)}

    assert statuses["home_peer_id"] == "ok"


# ── Finding 5: the picker yields to core's usable fallback ───────────────


@pytest.mark.asyncio
async def test_an_unsupported_client_gets_no_adapter_owned_picker():
    """Core only returns early on success, so non-success runs its own list."""
    adapter = make_adapter()  # no capabilities recorded

    result = await VKAdapter.send_model_picker(
        adapter,
        chat_id=str(DM_PEER),
        providers=[{"slug": "p", "name": "P", "models": ["vendor/model-a"]}],
        current_model="vendor/model-a",
        current_provider="p",
        session_key=f"agent:main:vk:dm:{DM_PEER}",
        on_model_selected=None,
    )

    assert not result.success
    assert adapter.client.sends == [], "an unusable picker was sent anyway"


def test_core_falls_back_when_the_picker_reports_non_success():
    """Contract: gateway/slash_commands.py returns early only on success."""
    import inspect

    from gateway.slash_commands import GatewaySlashCommandsMixin

    source = inspect.getsource(GatewaySlashCommandsMixin._handle_model_command)
    assert "send_model_picker" in source
    assert "if result.success:" in source
    assert "return None" in source


@pytest.mark.asyncio
async def test_a_capable_client_still_gets_the_button_picker():
    adapter = make_adapter()
    VKAdapter._remember_capabilities(adapter, DM_PEER, MODERN)

    result = await VKAdapter.send_model_picker(
        adapter,
        chat_id=str(DM_PEER),
        providers=[{"slug": "p", "name": "P", "models": ["vendor/model-a"]}],
        current_model="vendor/model-a",
        current_provider="p",
        session_key=f"agent:main:vk:dm:{DM_PEER}",
        on_model_selected=None,
    )

    assert result.success
    assert adapter.client.sends[0]["keyboard"]


# ── cleanup: the dead idempotency cache is gone ─────────────────────────


def test_the_dead_outbound_idempotency_cache_is_removed():
    import inspect

    from plugins.vk import adapter as adapter_module
    from plugins.vk import utils as utils_module

    blob = inspect.getsource(adapter_module) + inspect.getsource(utils_module)
    assert "_outbound_random_ids" not in blob
    assert "OUTBOUND_IDEMPOTENCY" not in blob
