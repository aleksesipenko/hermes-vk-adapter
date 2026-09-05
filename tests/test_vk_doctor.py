"""Read-only VK diagnostics that never claim more than they proved."""

from __future__ import annotations

import pytest

from plugins.vk.doctor import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_WARNING,
    REQUIRED_LONGPOLL_EVENTS,
    REQUIRED_SCOPES,
    CheckResult,
    DoctorReport,
    build_parser,
    run_live_checks,
    run_local_checks,
    run_send_smoke,
)

TOKEN = "a" * 85
GOOD_ENV = {"VK_GROUP_TOKEN": TOKEN, "VK_GROUP_ID": "123456789", "VK_API_VERSION": "5.199"}


class FakeClient:
    def __init__(self, **responses):
        self.responses = responses
        self.calls = []

    async def call(self, method, **params):
        self.calls.append((method, params))
        value = self.responses.get(method)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise AssertionError(f"unexpected call: {method}")
        return value

    async def send_message(self, **kwargs):
        self.calls.append(("messages.send", kwargs))
        return 4242


def healthy_client(**overrides):
    responses = {
        "groups.getTokenPermissions": {
            "mask": 1,
            "settings": [{"setting": 1, "name": name} for name in REQUIRED_SCOPES],
        },
        "groups.getLongPollSettings": {
            "is_enabled": True,
            "api_version": "5.199",
            "events": dict.fromkeys(REQUIRED_LONGPOLL_EVENTS, 1),
        },
        "groups.getLongPollServer": {"server": "https://lp.vk", "key": "k", "ts": "1"},
    }
    responses.update(overrides)
    return FakeClient(**responses)


def statuses(results):
    return {result.name: result.status for result in results}


# ── local checks ──────────────────────────────────────────────────────────


def test_a_valid_local_configuration_passes():
    results = run_local_checks(GOOD_ENV)

    # "skip" is a legitimate outcome (an unset optional), only fail/warn matter.
    assert not [r for r in results if r.status in {"fail", "warn"}], statuses(results)
    assert DoctorReport(results).exit_code == EXIT_OK


def test_local_checks_never_print_the_token():
    rendered = DoctorReport(run_local_checks(GOOD_ENV)).render()

    assert TOKEN not in rendered
    assert "set" in rendered.lower()


@pytest.mark.parametrize("group_id", ["-123", "0", "club123", "", "12.5", "  "])
def test_a_non_positive_numeric_group_id_fails(group_id):
    results = run_local_checks({**GOOD_ENV, "VK_GROUP_ID": group_id})

    assert statuses(results)["group_id"] == "fail"


@pytest.mark.parametrize("version", ["5", "abc", "5.x", "v5.199", "5.1.9"])
def test_a_malformed_api_version_fails(version):
    results = run_local_checks({**GOOD_ENV, "VK_API_VERSION": version})

    assert statuses(results)["api_version"] == "fail"


def test_a_missing_token_fails_without_naming_a_value():
    results = run_local_checks({**GOOD_ENV, "VK_GROUP_TOKEN": ""})

    assert statuses(results)["token"] == "fail"


def test_an_absent_api_version_falls_back_to_the_default():
    env = {key: value for key, value in GOOD_ENV.items() if key != "VK_API_VERSION"}

    assert statuses(run_local_checks(env))["api_version"] == "ok"


# ── live checks ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_healthy_community_reports_all_ok():
    results = await run_live_checks(healthy_client())

    assert not [r for r in results if r.status in {"fail", "warn"}], statuses(results)
    assert DoctorReport(results).exit_code == EXIT_OK


@pytest.mark.asyncio
async def test_a_missing_scope_is_reported_as_a_failure():
    client = healthy_client(
        **{
            "groups.getTokenPermissions": {
                "settings": [{"setting": 1, "name": "messages"}],
            }
        }
    )

    results = await run_live_checks(client)

    assert statuses(results)["token_permissions"] == "fail"
    assert "docs" in dict((r.name, r.detail) for r in results)["token_permissions"]


@pytest.mark.asyncio
async def test_excess_scopes_are_a_warning_not_a_failure():
    client = healthy_client(
        **{
            "groups.getTokenPermissions": {
                "settings": [
                    {"setting": 1, "name": name} for name in [*REQUIRED_SCOPES, "wall", "market"]
                ],
            }
        }
    )

    results = await run_live_checks(client)

    assert statuses(results)["token_permissions"] == "warn"
    assert DoctorReport(results).exit_code == EXIT_WARNING


@pytest.mark.asyncio
async def test_missing_long_poll_events_are_reported():
    client = healthy_client(
        **{
            "groups.getLongPollSettings": {
                "is_enabled": True,
                "api_version": "5.199",
                "events": {"message_new": 1},
            }
        }
    )

    results = await run_live_checks(client)

    assert statuses(results)["longpoll_settings"] == "fail"
    assert "message_event" in dict((r.name, r.detail) for r in results)["longpoll_settings"]


@pytest.mark.asyncio
async def test_long_poll_disabled_is_reported():
    client = healthy_client(
        **{"groups.getLongPollSettings": {"is_enabled": False, "events": {}}}
    )

    results = await run_live_checks(client)

    assert statuses(results)["longpoll_settings"] == "fail"


@pytest.mark.asyncio
async def test_a_long_poll_api_version_mismatch_warns():
    client = healthy_client(
        **{
            "groups.getLongPollSettings": {
                "is_enabled": True,
                "api_version": "5.131",
                "events": dict.fromkeys(REQUIRED_LONGPOLL_EVENTS, 1),
            }
        }
    )

    results = await run_live_checks(client, api_version="5.199")

    assert statuses(results)["longpoll_version"] == "warn"


@pytest.mark.asyncio
async def test_an_invalid_token_is_a_failure_not_a_crash():
    from plugins.vk.client import VKApiError

    error = VKApiError(
        "groups.getTokenPermissions", {"error": {"error_code": 5, "error_msg": "auth"}}
    )
    client = healthy_client(**{"groups.getTokenPermissions": error})

    results = await run_live_checks(client)

    assert statuses(results)["token_permissions"] == "fail"


@pytest.mark.asyncio
async def test_an_unreachable_api_is_a_failure_not_a_crash():
    import httpx

    client = healthy_client(**{"groups.getLongPollServer": httpx.ConnectError("no route")})

    results = await run_live_checks(client)

    assert statuses(results)["longpoll_server"] == "fail"


@pytest.mark.asyncio
async def test_live_output_never_contains_the_token():
    error_client = healthy_client(
        **{"groups.getLongPollServer": RuntimeError(f"failed with {TOKEN}")}
    )

    report = DoctorReport(await run_live_checks(error_client))

    assert TOKEN not in report.render()


@pytest.mark.asyncio
async def test_the_home_peer_check_is_skipped_when_unset():
    results = await run_live_checks(healthy_client(), home_peer_id=None)

    assert statuses(results).get("home_peer") == "skip"


@pytest.mark.asyncio
async def test_an_absent_home_peer_is_reported_without_failing_the_run():
    client = healthy_client(**{"messages.getConversationsById": {"items": []}})

    results = await run_live_checks(client, home_peer_id=2_000_000_001)

    assert statuses(results)["home_peer"] == "warn"


@pytest.mark.asyncio
async def test_a_resolvable_home_peer_passes():
    client = healthy_client(
        **{
            "messages.getConversationsById": {
                "items": [{"peer": {"id": 2_000_000_001, "type": "chat"}}]
            }
        }
    )

    results = await run_live_checks(client, home_peer_id=2_000_000_001)

    assert statuses(results)["home_peer"] == "ok"


# ── modes and exit status ─────────────────────────────────────────────────


def test_the_default_mode_is_read_only():
    args = build_parser().parse_args([])

    assert args.live is False
    assert args.send_smoke is False


def test_a_send_smoke_needs_both_the_flag_and_a_target():
    args = build_parser().parse_args(["--live", "--send-smoke", "--peer-id", "42"])

    assert args.send_smoke is True
    assert args.peer_id == 42


@pytest.mark.asyncio
async def test_a_send_smoke_without_a_target_refuses_to_send():
    client = healthy_client()

    result = await run_send_smoke(client, peer_id=None)

    assert result.status == "fail"
    assert not any(call[0] == "messages.send" for call in client.calls)


@pytest.mark.asyncio
async def test_a_send_smoke_with_a_target_sends_exactly_one_message():
    client = healthy_client()

    result = await run_send_smoke(client, peer_id=42)

    assert result.status == "ok"
    assert [call[0] for call in client.calls] == ["messages.send"]


def test_exit_status_distinguishes_ok_warning_and_failure():
    assert DoctorReport([CheckResult("a", "ok", "")]).exit_code == EXIT_OK
    assert DoctorReport([CheckResult("a", "warn", "")]).exit_code == EXIT_WARNING
    assert DoctorReport([CheckResult("a", "skip", "")]).exit_code == EXIT_OK
    assert (
        DoctorReport([CheckResult("a", "warn", ""), CheckResult("b", "fail", "")]).exit_code
        == EXIT_FAILURE
    )


def test_the_report_is_machine_readable():
    report = DoctorReport([CheckResult("a", "ok", "fine"), CheckResult("b", "fail", "broken")])

    payload = report.as_dict()

    assert payload["exit_code"] == EXIT_FAILURE
    assert payload["checks"][1] == {"name": "b", "status": "fail", "detail": "broken"}


def test_check_requirements_stays_offline():
    """`hermes status` calls it synchronously; it must not touch the network."""
    import inspect

    from plugins.vk import adapter

    source = inspect.getsource(adapter.check_requirements)

    assert "await" not in source
    assert "httpx.get" not in source and "requests" not in source


def test_the_plugin_registers_the_doctor_cli_command():
    from plugins.vk.adapter import register

    class Ctx:
        def __init__(self):
            self.platforms = []
            self.commands = []

        def register_platform(self, **kwargs):
            self.platforms.append(kwargs)

        def register_cli_command(self, **kwargs):
            self.commands.append(kwargs)

    ctx = Ctx()
    register(ctx)

    assert len(ctx.commands) == 1
    command = ctx.commands[0]
    assert command["name"] == "vk-doctor"
    assert callable(command["setup_fn"])
    assert callable(command["handler_fn"])
