"""Typed VK errors, redaction, and idempotent bounded retries.

These tests exercise the raw client only -- no Hermes import -- so they run in
any environment.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from httpx import Response

from plugins.vk.client import (
    VK_RETRYABLE_ERROR_CODES,
    VKApiError,
    VKRestClient,
    classify_vk_error_kind,
)
from plugins.vk.utils import redact_secrets

TOKEN = "a" * 85  # shaped like a real VK community token; not a real secret


def make_client(**kwargs) -> VKRestClient:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = VKRestClient(
        TOKEN,
        group_id=123456789,
        api_version="5.199",
        sleep=fake_sleep,
        **kwargs,
    )
    client.recorded_sleeps = sleeps  # type: ignore[attr-defined]
    return client


# ── typed errors ──────────────────────────────────────────────────────────


def test_vk_api_error_exposes_validated_numeric_code_and_message():
    error = VKApiError(
        "messages.send",
        {
            "error": {
                "error_code": 901,
                "error_msg": "Can't send messages for users without permission",
            }
        },
    )

    assert error.code == 901
    assert error.method == "messages.send"
    assert "without permission" in error.message
    assert "901" in str(error)


def test_vk_api_error_defaults_to_zero_for_missing_or_garbage_code():
    assert VKApiError("m", {"error": {"error_msg": "boom"}}).code == 0
    assert VKApiError("m", {"error": {"error_code": "not-a-number"}}).code == 0
    assert VKApiError("m", {}).code == 0
    assert VKApiError("m", {"error": "flat string"}).code == 0


def test_vk_api_error_never_retains_the_request_payload():
    """VK echoes the request -- including access_token -- in request_params."""
    error = VKApiError(
        "messages.send",
        {
            "error": {
                "error_code": 15,
                "error_msg": "Access denied",
                "request_params": [
                    {"key": "access_token", "value": TOKEN},
                    {"key": "message", "value": "private user text"},
                ],
            }
        },
    )

    blob = repr(error.__dict__) + str(error)
    assert TOKEN not in blob
    assert "private user text" not in blob
    assert not hasattr(error, "payload")


def test_vk_api_error_redacts_token_shaped_values_inside_the_message():
    payload = {"error": {"error_code": 5, "error_msg": f"bad token {TOKEN}"}}
    error = VKApiError("messages.send", payload)

    assert TOKEN not in error.message
    assert TOKEN not in str(error)


def test_vk_api_error_message_is_bounded():
    error = VKApiError("m", {"error": {"error_code": 1, "error_msg": "x" * 5000}})

    assert len(error.message) <= 300


def test_redact_secrets_masks_long_opaque_runs_but_keeps_prose():
    assert redact_secrets(f"token={TOKEN}") == "token=***"
    assert redact_secrets("peer_id=2000000001 is fine") == "peer_id=2000000001 is fine"
    assert redact_secrets("") == ""


# ── classification ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        (6, "rate_limited"),
        (9, "rate_limited"),
        (5, "forbidden"),
        (15, "forbidden"),
        (901, "forbidden"),
        (914, "too_long"),
        (1, "transient"),
        (10, "transient"),
    ],
)
def test_known_vk_codes_map_to_hermes_error_kinds(code: int, kind: str):
    error = VKApiError("messages.send", {"error": {"error_code": code}})

    assert classify_vk_error_kind(error) == kind


def test_unknown_vk_codes_are_conservative():
    """An unrecognised code must never be reported as safe-to-retry."""
    error = VKApiError("messages.send", {"error": {"error_code": 4242}})

    assert classify_vk_error_kind(error) == "unknown"
    assert 4242 not in VK_RETRYABLE_ERROR_CODES


def test_transport_failures_classify_as_transient():
    assert classify_vk_error_kind(httpx.ConnectTimeout("timed out")) == "transient"
    assert classify_vk_error_kind(httpx.ReadError("reset")) == "transient"


# ── random_id / idempotency ───────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_send_message_uses_the_caller_supplied_random_id():
    client = make_client()
    route = respx.post("https://api.vk.com/method/messages.send").mock(
        return_value=Response(200, json={"response": 42})
    )

    try:
        assert await client.send_message(peer_id=1, message="hi", random_id=987654) == 42
    finally:
        await client.close()

    assert b"random_id=987654" in route.calls.last.request.content


@pytest.mark.asyncio
@respx.mock
async def test_send_message_generates_a_nonzero_random_id_when_absent():
    client = make_client()
    route = respx.post("https://api.vk.com/method/messages.send").mock(
        return_value=Response(200, json={"response": 1})
    )

    try:
        await client.send_message(peer_id=1, message="hi")
    finally:
        await client.close()

    body = route.calls.last.request.content.decode()
    random_id = int(dict(pair.split("=", 1) for pair in body.split("&"))["random_id"])
    assert random_id > 0


def test_new_random_id_is_always_a_positive_int32():
    client = make_client()
    values = {client.new_random_id() for _ in range(500)}

    assert all(0 < value <= 2_147_483_647 for value in values)
    assert len(values) > 1


@pytest.mark.asyncio
@respx.mock
async def test_timeout_retry_never_changes_the_random_id():
    """A timeout may mean VK accepted the send. A new id would duplicate it."""
    client = make_client()
    attempts: list[bytes] = []

    def responder(request: httpx.Request) -> Response:
        attempts.append(request.content)
        if len(attempts) < 3:
            raise httpx.ReadTimeout("timed out", request=request)
        return Response(200, json={"response": 7})

    respx.post("https://api.vk.com/method/messages.send").mock(side_effect=responder)

    try:
        assert await client.send_message(peer_id=1, message="hi", random_id=555) == 7
    finally:
        await client.close()

    assert len(attempts) == 3
    assert all(b"random_id=555" in body for body in attempts)


@pytest.mark.asyncio
@respx.mock
async def test_retryable_vk_error_code_is_retried_with_the_same_random_id():
    client = make_client()
    attempts: list[bytes] = []

    def responder(request: httpx.Request) -> Response:
        attempts.append(request.content)
        if len(attempts) == 1:
            too_many = {"error_code": 6, "error_msg": "Too many requests"}
            return Response(200, json={"error": too_many})
        return Response(200, json={"response": 9})

    respx.post("https://api.vk.com/method/messages.send").mock(side_effect=responder)

    try:
        assert await client.send_message(peer_id=1, message="hi", random_id=111) == 9
    finally:
        await client.close()

    assert len(attempts) == 2
    assert all(b"random_id=111" in body for body in attempts)


@pytest.mark.asyncio
@respx.mock
async def test_permanent_vk_error_is_not_retried():
    client = make_client()
    route = respx.post("https://api.vk.com/method/messages.send").mock(
        return_value=Response(
            200, json={"error": {"error_code": 901, "error_msg": "no permission"}}
        )
    )

    try:
        with pytest.raises(VKApiError) as excinfo:
            await client.send_message(peer_id=1, message="hi", random_id=1)
    finally:
        await client.close()

    assert excinfo.value.code == 901
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_retries_are_bounded_and_raise_the_last_error():
    client = make_client()
    route = respx.post("https://api.vk.com/method/messages.send").mock(
        side_effect=httpx.ConnectError("refused")
    )

    try:
        with pytest.raises(httpx.ConnectError):
            await client.send_message(peer_id=1, message="hi", random_id=1, max_attempts=3)
    finally:
        await client.close()

    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_backoff_is_bounded_and_never_sleeps_for_real():
    client = make_client()
    respx.post("https://api.vk.com/method/messages.send").mock(
        side_effect=httpx.ConnectError("refused")
    )

    try:
        with pytest.raises(httpx.ConnectError):
            await client.send_message(peer_id=1, message="hi", random_id=1, max_attempts=4)
    finally:
        await client.close()

    delays = client.recorded_sleeps
    assert len(delays) == 3  # one per retry, not after the final failure
    assert delays == sorted(delays)
    assert all(0 < delay <= 30 for delay in delays)


# ── calls that must never be blindly retried ──────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_document_upload_is_not_retried_on_timeout(tmp_path):
    """A retried upload could attach the same file twice; safety is unclear."""
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello", encoding="utf-8")

    client = make_client()
    route = respx.post("https://api.vk.com/method/docs.getMessagesUploadServer").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.upload_document_raw(peer_id=1, path=str(file_path))
    finally:
        await client.close()

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_edit_message_is_not_retried_on_timeout():
    client = make_client()
    route = respx.post("https://api.vk.com/method/messages.edit").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.edit_message(peer_id=1, message_id=2, message="x")
    finally:
        await client.close()

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_callback_answer_is_not_retried_on_timeout():
    client = make_client()
    route = respx.post("https://api.vk.com/method/messages.sendMessageEventAnswer").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.send_message_event_answer(event_id="e", user_id=1, peer_id=1, text="t")
    finally:
        await client.close()

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_api_errors_do_not_leak_the_token_into_the_raised_exception():
    client = make_client()
    respx.post("https://api.vk.com/method/messages.send").mock(
        return_value=Response(
            200,
            json={
                "error": {
                    "error_code": 15,
                    "error_msg": "Access denied",
                    "request_params": [{"key": "access_token", "value": TOKEN}],
                }
            },
        )
    )

    try:
        with pytest.raises(VKApiError) as excinfo:
            await client.send_message(peer_id=1, message="hi", random_id=1)
    finally:
        await client.close()

    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in repr(excinfo.value.__dict__)
