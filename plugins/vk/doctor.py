"""`hermes vk-doctor` -- diagnostics that distinguish local from live.

``check_requirements()`` can only say "the module imports and the environment
variables exist".  That is exactly the false green the operator hit: the plugin
reported healthy while VK was refusing the token.  This command separates three
modes so a passing run means something specific:

* **local** (default): configuration shape only, no network at all.
* **live** (``--live``): read-only VK calls -- permissions, Long Poll settings
  and reachability. Nothing is created, edited or sent.
* **send smoke** (``--live --send-smoke --peer-id N``): the only mode that
  writes anything, and it needs both the flag and an explicit target.

Output is redacted: the token is never printed, only whether it is set.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any

from .utils import DEFAULT_API_VERSION, redact_secrets

EXIT_OK = 0
EXIT_WARNING = 1
EXIT_FAILURE = 2

#: Least privilege for this adapter: Long Poll, messaging, and the two upload
#: surfaces. Anything beyond this is reported as excess.
REQUIRED_SCOPES = ("messages", "manage", "docs", "photos")

#: Without these two the adapter cannot receive messages or button presses.
REQUIRED_LONGPOLL_EVENTS = ("message_new", "message_event")

_API_VERSION_RE = re.compile(r"^\d+\.\d+$")

_STATUS_MARK = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "skip": "skip"}


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # "ok" | "warn" | "fail" | "skip"
    detail: str


class DoctorReport:
    def __init__(self, results: list[CheckResult]) -> None:
        self.results = list(results)

    @property
    def exit_code(self) -> int:
        if any(result.status == "fail" for result in self.results):
            return EXIT_FAILURE
        if any(result.status == "warn" for result in self.results):
            return EXIT_WARNING
        return EXIT_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "checks": [
                {"name": r.name, "status": r.status, "detail": r.detail} for r in self.results
            ],
        }

    def render(self) -> str:
        lines = [
            f"[{_STATUS_MARK.get(r.status, r.status)}] {r.name}: {redact_secrets(r.detail)}"
            for r in self.results
        ]
        return "\n".join(lines)


def run_local_checks(env: dict[str, str] | None = None) -> list[CheckResult]:
    """Configuration shape only. Never performs I/O."""
    source = env if env is not None else os.environ
    results: list[CheckResult] = []

    token = str(source.get("VK_GROUP_TOKEN") or "").strip()
    results.append(
        CheckResult("token", "ok", "VK_GROUP_TOKEN is set")
        if token
        else CheckResult("token", "fail", "VK_GROUP_TOKEN is not set")
    )

    raw_group = str(source.get("VK_GROUP_ID") or "").strip()
    if raw_group.isdigit() and int(raw_group) > 0:
        results.append(CheckResult("group_id", "ok", f"VK_GROUP_ID={raw_group}"))
    else:
        results.append(
            CheckResult(
                "group_id",
                "fail",
                "VK_GROUP_ID must be a positive number without a minus sign "
                "(club123456789 -> 123456789)",
            )
        )

    version = str(source.get("VK_API_VERSION") or "").strip() or DEFAULT_API_VERSION
    if _API_VERSION_RE.match(version):
        results.append(CheckResult("api_version", "ok", f"VK_API_VERSION={version}"))
    else:
        results.append(
            CheckResult("api_version", "fail", "VK_API_VERSION must look like 5.199")
        )

    home = str(source.get("VK_HOME_PEER_ID") or "").strip()
    results.append(
        CheckResult("home_peer_id", "ok", f"VK_HOME_PEER_ID={home}")
        if home
        else CheckResult("home_peer_id", "skip", "VK_HOME_PEER_ID is not set (cron delivery off)")
    )
    return results


async def _guarded(name: str, coro) -> tuple[CheckResult | None, Any]:
    """Run one live probe; turn any failure into a redacted CheckResult."""
    try:
        return None, await coro
    except Exception as exc:
        return CheckResult(name, "fail", f"{type(exc).__name__}: {redact_secrets(exc)}"), None


async def run_live_checks(
    client: Any,
    *,
    api_version: str = DEFAULT_API_VERSION,
    home_peer_id: int | None = None,
) -> list[CheckResult]:
    """Read-only VK probes. Creates, edits and sends nothing."""
    results: list[CheckResult] = []

    failure, permissions = await _guarded(
        "token_permissions", client.call("groups.getTokenPermissions")
    )
    if failure:
        results.append(failure)
    else:
        results.append(_check_permissions(permissions))

    failure, settings = await _guarded(
        "longpoll_settings", client.call("groups.getLongPollSettings", group_id=None)
    )
    if failure:
        results.append(failure)
    else:
        results.extend(_check_longpoll_settings(settings, api_version))

    failure, server = await _guarded(
        "longpoll_server", client.call("groups.getLongPollServer", group_id=None)
    )
    if failure:
        results.append(failure)
    elif isinstance(server, dict) and server.get("server") and server.get("key"):
        results.append(CheckResult("longpoll_server", "ok", "Long Poll server is reachable"))
    else:
        results.append(
            CheckResult("longpoll_server", "fail", "groups.getLongPollServer returned no server")
        )

    results.append(await _check_home_peer(client, home_peer_id))
    return results


def _check_permissions(permissions: Any) -> CheckResult:
    settings = (permissions or {}).get("settings")
    granted = {
        str(entry.get("name"))
        for entry in (settings or [])
        if isinstance(entry, dict) and entry.get("name")
    }
    if not granted:
        return CheckResult(
            "token_permissions", "fail", "groups.getTokenPermissions listed no scopes"
        )
    missing = [scope for scope in REQUIRED_SCOPES if scope not in granted]
    if missing:
        return CheckResult(
            "token_permissions", "fail", f"missing scopes: {', '.join(missing)}"
        )
    excess = sorted(granted - set(REQUIRED_SCOPES))
    if excess:
        return CheckResult(
            "token_permissions",
            "warn",
            f"token has scopes this adapter never uses: {', '.join(excess)}",
        )
    return CheckResult("token_permissions", "ok", f"scopes: {', '.join(REQUIRED_SCOPES)}")


def _check_longpoll_settings(settings: Any, api_version: str) -> list[CheckResult]:
    settings = settings if isinstance(settings, dict) else {}
    results: list[CheckResult] = []

    if not settings.get("is_enabled"):
        results.append(
            CheckResult(
                "longpoll_settings",
                "fail",
                "Long Poll API is disabled in the community settings",
            )
        )
    else:
        events = settings.get("events") if isinstance(settings.get("events"), dict) else {}
        missing = [name for name in REQUIRED_LONGPOLL_EVENTS if not events.get(name)]
        if missing:
            results.append(
                CheckResult(
                    "longpoll_settings", "fail", f"missing Long Poll events: {', '.join(missing)}"
                )
            )
        else:
            results.append(
                CheckResult("longpoll_settings", "ok", "Long Poll enabled with required events")
            )

    reported = str(settings.get("api_version") or "").strip()
    if not reported:
        results.append(
            CheckResult("longpoll_version", "skip", "community reported no Long Poll version")
        )
    elif reported != api_version:
        results.append(
            CheckResult(
                "longpoll_version",
                "warn",
                f"community Long Poll version {reported} != VK_API_VERSION {api_version}",
            )
        )
    else:
        results.append(CheckResult("longpoll_version", "ok", f"Long Poll version {reported}"))
    return results


async def _check_home_peer(client: Any, home_peer_id: int | None) -> CheckResult:
    if not home_peer_id:
        return CheckResult("home_peer", "skip", "no home peer configured")
    failure, conversation = await _guarded(
        "home_peer", client.call("messages.getConversationsById", peer_ids=home_peer_id)
    )
    if failure:
        return failure
    items = (conversation or {}).get("items") or []
    if not items:
        return CheckResult(
            "home_peer",
            "warn",
            f"peer {home_peer_id} is not a conversation this community can see yet",
        )
    return CheckResult("home_peer", "ok", f"home peer {home_peer_id} resolved")


async def run_send_smoke(client: Any, *, peer_id: int | None) -> CheckResult:
    """The only mode that writes. Requires an explicit target."""
    if not peer_id:
        return CheckResult(
            "send_smoke", "fail", "--send-smoke requires --peer-id; refusing to guess a target"
        )
    try:
        response = await client.send_message(
            peer_id=int(peer_id), message="Hermes VK adapter doctor: outbound send works."
        )
    except Exception as exc:
        return CheckResult("send_smoke", "fail", f"{type(exc).__name__}: {redact_secrets(exc)}")
    return CheckResult("send_smoke", "ok", f"sent message id {response}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes vk-doctor")
    register_cli(parser)
    return parser


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Wire up `hermes vk-doctor` (called via ctx.register_cli_command)."""
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also run read-only VK API checks (permissions, Long Poll, reachability)",
    )
    parser.add_argument(
        "--send-smoke",
        action="store_true",
        help="Send one real message. Requires --live and --peer-id.",
    )
    parser.add_argument(
        "--peer-id", type=int, default=None, help="Target peer id for --send-smoke"
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")


def dispatch(args: argparse.Namespace) -> int:
    return asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> int:
    import json as _json

    results = run_local_checks()
    if getattr(args, "live", False) and DoctorReport(results).exit_code != EXIT_FAILURE:
        from .client import VKRestClient

        client = VKRestClient(
            os.getenv("VK_GROUP_TOKEN", ""),
            int(os.getenv("VK_GROUP_ID", "0") or 0),
            os.getenv("VK_API_VERSION", DEFAULT_API_VERSION),
        )
        try:
            home = os.getenv("VK_HOME_PEER_ID", "").strip()
            results += await run_live_checks(
                client,
                api_version=os.getenv("VK_API_VERSION", DEFAULT_API_VERSION),
                home_peer_id=int(home) if home.isdigit() else None,
            )
            if getattr(args, "send_smoke", False):
                results.append(await run_send_smoke(client, peer_id=args.peer_id))
        finally:
            await client.close()
    elif getattr(args, "send_smoke", False):
        results.append(
            CheckResult("send_smoke", "fail", "--send-smoke requires --live")
        )

    report = DoctorReport(results)
    print(_json.dumps(report.as_dict(), ensure_ascii=False) if args.json else report.render())
    return report.exit_code
