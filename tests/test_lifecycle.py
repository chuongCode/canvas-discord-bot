"""The real `app.main.run()` lifecycle: bind, serve, SIGTERM, release.

These exercise the actual entrypoint. Only the Discord gateway is stubbed —
everything else (PostgreSQL, the health server, signal handling, cleanup) is
the code that runs in production.
"""

from __future__ import annotations

import asyncio
import json
import signal
from dataclasses import replace

import aiohttp
import discord
import pytest

from app import main as app_main
from tests.test_health import HOST, free_port

pytestmark = pytest.mark.filterwarnings("ignore::RuntimeWarning")


@pytest.fixture
def serving_config(config):
    return replace(config, http_host=HOST, http_port=free_port())


def stub_gateway(monkeypatch, *, closes_cleanly: bool = True):
    """Replace Client.start with something that behaves like a live gateway.

    ``closes_cleanly=False`` models a wedged websocket that ignores close(),
    which is what the shutdown grace period exists for.
    """
    started = asyncio.Event()

    async def fake_start(self, token, *, reconnect=True):
        started.set()
        if closes_cleanly:
            while not self.is_closed():
                await asyncio.sleep(0.01)
        else:
            await asyncio.Event().wait()

    monkeypatch.setattr(discord.Client, "start", fake_start)
    return started


async def get_health(port: int) -> tuple[int, dict]:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://{HOST}:{port}/health") as response:
            return response.status, json.loads(await response.text())


async def test_run_serves_health_then_exits_zero_on_sigterm(serving_config, monkeypatch):
    started = stub_gateway(monkeypatch)
    task = asyncio.create_task(app_main.run(serving_config))
    await asyncio.wait_for(started.wait(), timeout=10)

    status, payload = await get_health(serving_config.http_port)
    assert status == 200
    assert payload["database"] == "ok"

    signal.raise_signal(signal.SIGTERM)
    assert await asyncio.wait_for(task, timeout=15) == 0

    # The listener must be gone, or the next deploy cannot claim the port.
    with pytest.raises((aiohttp.ClientError, OSError)):
        await get_health(serving_config.http_port)


async def test_a_wedged_gateway_still_shuts_down(serving_config, monkeypatch):
    """Shutdown is bounded, so the platform never has to SIGKILL us."""
    monkeypatch.setattr(app_main, "SHUTDOWN_GRACE_SECONDS", 0.2)
    started = stub_gateway(monkeypatch, closes_cleanly=False)
    task = asyncio.create_task(app_main.run(serving_config))
    await asyncio.wait_for(started.wait(), timeout=10)

    signal.raise_signal(signal.SIGTERM)
    assert await asyncio.wait_for(task, timeout=15) == 0


async def test_a_rejected_discord_token_exits_non_zero(serving_config, monkeypatch):
    async def reject(self, token, *, reconnect=True):
        raise discord.LoginFailure("bad token")

    monkeypatch.setattr(discord.Client, "start", reject)
    assert await app_main.run(serving_config) == 1


async def test_an_unusable_port_aborts_startup(serving_config, monkeypatch):
    """A web service that cannot bind $PORT is a failed deploy, not a warning."""
    stub_gateway(monkeypatch)
    blocker = await asyncio.start_server(
        lambda r, w: None, HOST, serving_config.http_port
    )
    try:
        assert await app_main.run(serving_config) == 1
    finally:
        blocker.close()
        await blocker.wait_closed()


async def test_an_unreachable_database_exits_non_zero(serving_config, monkeypatch):
    from app.database import Database

    stub_gateway(monkeypatch)
    # The production 30s grace exists for a managed database that is still
    # waking up; the behaviour under test is the exit code, not the wait.
    monkeypatch.setattr(
        app_main, "Database", lambda dsn: Database(dsn, connect_timeout=2.0)
    )
    broken = replace(
        serving_config, database_url="postgresql://nobody@127.0.0.1:1/none?connect_timeout=1"
    )
    assert await app_main.run(broken) == 1


async def test_sigterm_is_idempotent(serving_config, monkeypatch):
    started = stub_gateway(monkeypatch)
    task = asyncio.create_task(app_main.run(serving_config))
    await asyncio.wait_for(started.wait(), timeout=10)

    signal.raise_signal(signal.SIGTERM)
    signal.raise_signal(signal.SIGTERM)
    signal.raise_signal(signal.SIGINT)
    assert await asyncio.wait_for(task, timeout=15) == 0
