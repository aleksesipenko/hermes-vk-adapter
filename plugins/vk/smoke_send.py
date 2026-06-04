"""Outbound VK smoke for the Hermes VK adapter.

Run from this repository or from the deployed plugin directory:

    set -a
    source .env
    set +a
    python plugins/vk/smoke_send.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from plugins.vk.client import VKRestClient  # noqa: E402
else:
    from .client import VKRestClient


async def main() -> None:
    client = VKRestClient(
        token=os.environ["VK_GROUP_TOKEN"],
        group_id=int(os.environ["VK_GROUP_ID"]),
        api_version=os.getenv("VK_API_VERSION", "5.199"),
    )
    try:
        response = await client.send_message(
            peer_id=int(os.environ["VK_HOME_PEER_ID"]),
            message="Hermes VK adapter smoke: outbound messages.send works.",
        )
        print(response)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
