"""Long Poll ownership, truthful health, failure handling, and isolation."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from plugins.vk.adapter import VKAdapter
from plugins.vk.client import LongPollState
from plugins.vk.state import BoundedTTLCache

pytestmark = pytest.mark.hermes_contract


class FakeClient:
    """A VK client whose poll() replays a scripted list of results."""

    def __init__(self, results=None, states=None):
        self._results = list(results or [])
        self._states = list(states or [])
        self.polls = 0
        self.state_fetches = 0
        self.closed = False

    async def get_long_poll_state(self):
        self.state_fetches += 1
        if self._states:
            return self._states.pop(0)
        return LongPollState(server="s", key=f"k{self.state_fetches}", ts="100")

    async def poll(self, state, wait_seconds):
        self.polls += 1
        if not self._results:
            raise asyncio.CancelledError
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self):
        self.closed = True


def make_adapter(client=None, **overrides):
    adapter = object.__new__(VKAdapter)
    adapter.client = client
    adapter.group_id = 123456789
    adapter.token = "t" * 85
    adapter.api_version = "5.199"
    adapter.wait_seconds = 25
    adapter.debug_updates = False
    adapter.longpoll_state = LongPollState(server="s", key="k", ts="100")
    adapter._running = True
    adapter._poll_failures = 0
    adapter._last_successful_poll = None
    adapter._longpoll_degraded = False
    adapter._seen_events = BoundedTTLCache(max_entries=64, ttl_seconds=300)
    adapter._status_events = []
    adapter._fatal = []
    adapter.poll_task = None
    adapter._backoff_sleeps = []

    async def fake_sleep(delay):
        adapter._backoff_sleeps.append(delay)

    adapter._poll_sleep = fake_sleep
    adapter._mark_connected = lambda: adapter._status_events.append("connected")
    adapter._mark_disconnected = lambda: adapter._status_events.append("disconnected")
    adapter._write_runtime_status_safe = lambda context, **kw: adapter._status_events.append(
        kw.get("platform_state", context)
    )

    def set_fatal(code, message, *, retryable):
        adapter._fatal.append((code, message, retryable))
        adapter._running = False

    adapter._set_fatal_error = set_fatal
    for key, value in overrides.items():
        setattr(adapter, key, value)
    return adapter


async def drain(adapter):
    """Run the poll loop until its scripted script is exhausted."""
    with pytest.raises(asyncio.CancelledError):
        await VKAdapter._poll_loop(adapter)


# ── ownership ─────────────────────────────────────────────────────────────


def test_lock_identity_is_stable_and_never_contains_the_token():
    adapter = make_adapter()
    identity = VKAdapter._longpoll_lock_identity(adapter)

    assert identity == VKAdapter._longpoll_lock_identity(adapter)
    assert adapter.token not in identity
    assert str(adapter.group_id) in identity


def test_lock_identity_differs_per_group():
    first = make_adapter()
    second = make_adapter(group_id=987654321)

    assert VKAdapter._longpoll_lock_identity(first) != VKAdapter._longpoll_lock_identity(second)


@pytest.mark.asyncio
async def test_connect_refuses_to_poll_when_the_lock_is_held():
    adapter = make_adapter()
    adapter._acquire_platform_lock = lambda scope, identity, desc: False
    released = []
    adapter._release_platform_lock = lambda: released.append(True)

    assert await VKAdapter.connect(adapter) is False
    assert adapter.poll_task is None


@pytest.mark.asyncio
async def test_failed_connect_releases_the_lock():
    adapter = make_adapter()
    adapter._acquire_platform_lock = lambda scope, identity, desc: True
    released = []
    adapter._release_platform_lock = lambda: released.append(True)

    class ExplodingClient(FakeClient):
        async def get_long_poll_state(self):
            raise httpx.ConnectError("no route to VK")

    adapter._build_client = lambda: ExplodingClient()

    assert await VKAdapter.connect(adapter) is False
    assert released == [True]
    assert adapter.poll_task is None


@pytest.mark.asyncio
async def test_disconnect_releases_the_lock_and_closes_the_client():
    client = FakeClient()
    adapter = make_adapter(client)
    released = []
    adapter._release_platform_lock = lambda: released.append(True)

    await VKAdapter.disconnect(adapter)

    assert released == [True]
    assert client.closed is True


# ── truthful health ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transport_failure_marks_degraded_and_success_marks_connected_again():
    client = FakeClient(
        results=[
            httpx.ReadTimeout("timed out"),
            {"ts": "101", "updates": []},
        ]
    )
    adapter = make_adapter(client)

    await drain(adapter)

    assert "retrying" in adapter._status_events
    assert adapter._status_events.index("retrying") < adapter._status_events.index("connected")
    assert adapter._poll_failures == 0
    assert adapter._last_successful_poll is not None


@pytest.mark.asyncio
async def test_repeated_failures_do_not_rewrite_status_every_time():
    client = FakeClient(results=[httpx.ReadTimeout("x"), httpx.ReadTimeout("y")])
    adapter = make_adapter(client)

    await drain(adapter)

    assert adapter._status_events.count("retrying") == 1
    assert adapter._poll_failures == 2


@pytest.mark.asyncio
async def test_healthy_polls_do_not_rewrite_connected_status_every_time():
    client = FakeClient(results=[{"ts": "101", "updates": []}, {"ts": "102", "updates": []}])
    adapter = make_adapter(client)

    await drain(adapter)

    assert adapter._status_events.count("connected") == 0


# ── backoff ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backoff_grows_and_stays_bounded():
    client = FakeClient(results=[httpx.ConnectError("x") for _ in range(6)])
    adapter = make_adapter(client)

    await drain(adapter)

    delays = adapter._backoff_sleeps
    assert len(delays) == 6
    assert delays[0] < delays[-1]
    assert all(0 < delay <= VKAdapter.POLL_BACKOFF_MAX for delay in delays)


@pytest.mark.asyncio
async def test_backoff_resets_after_a_successful_poll():
    client = FakeClient(
        results=[
            httpx.ConnectError("a"),
            httpx.ConnectError("b"),
            {"ts": "101", "updates": []},
            httpx.ConnectError("c"),
        ]
    )
    adapter = make_adapter(client)

    await drain(adapter)

    delays = adapter._backoff_sleeps
    assert len(delays) == 3
    assert delays[2] <= delays[0] * 1.5  # back to the first-failure delay


# ── documented `failed` codes ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failed_1_only_advances_ts_without_refetching():
    client = FakeClient(results=[{"failed": 1, "ts": "555"}])
    adapter = make_adapter(client)

    await drain(adapter)

    assert adapter.longpoll_state.ts == "555"
    assert client.state_fetches == 0


@pytest.mark.asyncio
async def test_failed_2_renews_the_key_but_keeps_ts():
    client = FakeClient(
        results=[{"failed": 2}],
        states=[LongPollState(server="s2", key="new-key", ts="999")],
    )
    adapter = make_adapter(client)
    adapter.longpoll_state = LongPollState(server="s", key="old-key", ts="100")

    await drain(adapter)

    assert adapter.longpoll_state.key == "new-key"
    assert adapter.longpoll_state.ts == "100"


@pytest.mark.asyncio
async def test_failed_3_renews_both_the_key_and_ts():
    client = FakeClient(
        results=[{"failed": 3}],
        states=[LongPollState(server="s3", key="new-key", ts="999")],
    )
    adapter = make_adapter(client)
    adapter.longpoll_state = LongPollState(server="s", key="old-key", ts="100")

    await drain(adapter)

    assert adapter.longpoll_state.key == "new-key"
    assert adapter.longpoll_state.ts == "999"


@pytest.mark.asyncio
async def test_failed_4_is_a_non_looping_configuration_error():
    client = FakeClient(results=[{"failed": 4}, {"ts": "1", "updates": []}])
    adapter = make_adapter(client)

    await VKAdapter._poll_loop(adapter)

    assert adapter._fatal
    code, message, retryable = adapter._fatal[-1]
    assert code == "vk_longpoll_version"
    assert retryable is False
    assert "VK_API_VERSION" in message
    assert client.polls == 1  # stopped, did not spin


# ── per-update isolation and dedupe ───────────────────────────────────────


@pytest.mark.asyncio
async def test_one_bad_update_does_not_drop_later_updates():
    handled = []

    async def handle(update):
        if update["object"]["message"]["text"] == "boom":
            raise RuntimeError("handler exploded")
        handled.append(update["object"]["message"]["text"])

    updates = [
        {"type": "message_new", "object": {"message": {"id": 1, "peer_id": 5, "text": "first"}}},
        {"type": "message_new", "object": {"message": {"id": 2, "peer_id": 5, "text": "boom"}}},
        {"type": "message_new", "object": {"message": {"id": 3, "peer_id": 5, "text": "last"}}},
    ]
    client = FakeClient(results=[{"ts": "101", "updates": updates}])
    adapter = make_adapter(client)
    adapter._handle_update = handle

    await drain(adapter)

    assert handled == ["first", "last"]


@pytest.mark.asyncio
async def test_duplicate_updates_are_suppressed_within_the_window():
    handled = []

    async def handle(update):
        handled.append(update)

    duplicate = {
        "type": "message_new",
        "object": {"message": {"id": 7, "peer_id": 5, "conversation_message_id": 3}},
    }
    client = FakeClient(
        results=[
            {"ts": "101", "updates": [duplicate]},
            {"ts": "102", "updates": [duplicate]},
        ]
    )
    adapter = make_adapter(client)
    adapter._handle_update = handle

    await drain(adapter)

    assert len(handled) == 1


@pytest.mark.asyncio
async def test_distinct_updates_are_all_dispatched():
    handled = []

    async def handle(update):
        handled.append(update)

    client = FakeClient(
        results=[
            {
                "ts": "101",
                "updates": [
                    {"type": "message_new", "object": {"message": {"id": 1, "peer_id": 5}}},
                    {"type": "message_new", "object": {"message": {"id": 2, "peer_id": 5}}},
                    {"type": "message_event", "object": {"event_id": "e1", "peer_id": 5}},
                ],
            }
        ]
    )
    adapter = make_adapter(client)
    adapter._handle_update = handle

    await drain(adapter)

    assert len(handled) == 3


@pytest.mark.asyncio
async def test_updates_without_a_stable_identifier_are_never_deduped_away():
    handled = []

    async def handle(update):
        handled.append(update)

    anonymous = {"type": "group_change_settings", "object": {}}
    client = FakeClient(
        results=[
            {"ts": "101", "updates": [anonymous]},
            {"ts": "102", "updates": [anonymous]},
        ]
    )
    adapter = make_adapter(client)
    adapter._handle_update = handle

    await drain(adapter)

    assert len(handled) == 2


# ── task lifecycle ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ts_advances_only_from_successful_poll_results():
    client = FakeClient(results=[{"ts": "202", "updates": []}])
    adapter = make_adapter(client)

    await drain(adapter)

    assert adapter.longpoll_state.ts == "202"


@pytest.mark.asyncio
async def test_poll_task_failure_is_reported_instead_of_dying_silently():
    adapter = make_adapter()

    async def exploding():
        raise RuntimeError("poll loop died")

    task = asyncio.ensure_future(exploding())
    await asyncio.sleep(0)
    VKAdapter._on_poll_task_done(adapter, task)

    assert adapter._fatal
    code, _message, retryable = adapter._fatal[-1]
    assert code == "vk_longpoll_task_failed"
    assert retryable is True


@pytest.mark.asyncio
async def test_cancelled_poll_task_is_not_reported_as_a_failure():
    adapter = make_adapter()

    async def forever():
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(forever())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    VKAdapter._on_poll_task_done(adapter, task)

    assert adapter._fatal == []


@pytest.mark.asyncio
async def test_cancellation_stops_the_loop_promptly():
    client = FakeClient(results=[httpx.ConnectError("x")] * 100)
    adapter = make_adapter(client)

    async def real_sleep(delay):
        await asyncio.sleep(delay)

    adapter._poll_sleep = real_sleep
    task = asyncio.ensure_future(VKAdapter._poll_loop(adapter))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
