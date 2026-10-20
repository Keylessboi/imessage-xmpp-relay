"""Shared pytest fixtures.

Tests are plain sync functions that drive one asyncio.run() scenario each, so
the suite needs no pytest-asyncio plugin and no network access.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from relay.config import Config


def run(coro):
    """Run one async scenario to completion."""
    return asyncio.run(coro)


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> None:
    """Poll a predicate without ever hanging the suite."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met within " + str(timeout) + "s")
        await asyncio.sleep(interval)


def message_event(
    guid: str = "guid-1",
    *,
    sender: str = "+15551234567",
    text: str = "hello from iMessage",
    is_group: bool = False,
    sender_name: Optional[str] = "Alice",
    chat_guid: str = "chat-1",
) -> dict:
    return {
        "type": "message",
        "guid": guid,
        "chat_guid": chat_guid,
        "from": sender,
        "to": ["+15557654321"],
        "text": text,
        "timestamp": 1700000000,
        "is_group": is_group,
        "sender_name": sender_name,
    }


@pytest.fixture
def make_config():
    def _make(**overrides) -> Config:
        defaults = dict(
            jid="bot@example.org",
            password="s3cret",
            targets=("pipe@example.org",),
            allowed=frozenset({"alice@example.org"}),
        )
        defaults.update(overrides)
        return Config(**defaults)

    return _make
