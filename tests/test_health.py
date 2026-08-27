"""The HTTP health endpoint: binding, payload, status codes, shutdown.

These are the deployment-critical guarantees. If /health stops answering 200 on
$PORT, the platform marks the deploy unhealthy and the external keep-alive
monitor lets the service fall asleep.
"""

from __future__ import annotations

import asyncio
import json
import socket

import aiohttp
import psycopg
import pytest

from app.canvas_client import CanvasClient
from app.config import load_config
from app.database import Database, DatabaseUnavailable
from app.discord_bot import CanvasReminderBot
from app.health import HealthServer, HealthServerError

HOST = "127.0.0.1"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def bot(config):
    """A fully constructed, hydrated bot that has never touched the network."""
    db = Database(config.database_url)
    canvas = CanvasClient(config.canvas_base_url, config.canvas_api_token)
    instance = CanvasReminderBot(config, db, canvas)
    instance.service.hydrate()
    yield instance
    db.close()


async def serve(snapshot, port: int) -> HealthServer:
    server = HealthServer(snapshot, host=HOST, port=port)
    await server.start()
    return server


async def get(port: int, path: str = "/health") -> tuple[int, dict]:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://{HOST}:{port}{path}") as response:
            return response.status, json.loads(await response.text())


# ------------------------------------------------------------------ the basics


async def test_health_returns_200_with_a_json_body(bot):
    port = free_port()
    server = await serve(bot.health_snapshot, port)
    try:
        status, payload = await get(port)
    finally:
        await server.stop()

    assert status == 200
    assert payload["bot"] == "running"
    assert payload["database"] == "ok"
    assert payload["discord_connected"] is False
    assert payload["timestamp"].endswith("Z")
    assert "uptime_seconds" in payload
    assert "last_canvas_sync" in payload


async def test_root_and_healthz_are_aliases(bot):
    port = free_port()
    server = await serve(bot.health_snapshot, port)
    try:
        for path in ("/", "/healthz", "/health"):
            status, _ = await get(port, path)
            assert status == 200, path
    finally:
        await server.stop()


async def test_unknown_paths_are_404_not_a_crash(bot):
    port = free_port()
    server = await serve(bot.health_snapshot, port)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://{HOST}:{port}/nope") as response:
                assert response.status == 404
    finally:
        await server.stop()


async def test_health_reports_a_successful_canvas_sync(bot):
    from app.reminder_service import STATE_LAST_SYNC
    from tests.conftest import NOW

    # Persisted at the last real change, then reloaded — this is what a restart
    # sees, since the timestamp is no longer written as a five-minute heartbeat.
    bot.db.set_state_datetime(STATE_LAST_SYNC, NOW)
    bot.service.hydrate()
    bot.service.canvas_healthy = True

    port = free_port()
    server = await serve(bot.health_snapshot, port)
    try:
        _, payload = await get(port)
    finally:
        await server.stop()

    assert payload["last_canvas_sync"] == "2025-09-04T12:00:00Z"
    assert payload["canvas_healthy"] is True


async def test_health_never_leaks_credentials_or_ids(bot):
    port = free_port()
    server = await serve(bot.health_snapshot, port)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://{HOST}:{port}/health") as response:
                body = await response.text()
    finally:
        await server.stop()

    secrets = [
        bot.config.canvas_api_token,
        bot.config.discord_bot_token,
        str(bot.config.discord_channel_id),
        bot.config.database_url,
    ]
    for secret in secrets:
        assert secret not in body, "health payload must stay non-sensitive"


# ------------------------------------------------------------- degraded states


async def test_a_stopped_bot_reports_503(bot):
    await bot.close()
    port = free_port()
    server = await serve(bot.health_snapshot, port)
    try:
        status, payload = await get(port)
    finally:
        await server.stop()

    assert status == 503
    assert payload["status"] == "stopped"


async def test_an_unreachable_database_is_reported_but_still_200(bot):
    """A database blip must not make the platform kill a working process."""
    bot.db.close()
    with pytest.raises((psycopg.Error, DatabaseUnavailable)):
        bot.db.ping()          # the failure the endpoint reports second-hand

    port = free_port()
    server = await serve(bot.health_snapshot, port)
    try:
        status, payload = await get(port)
    finally:
        await server.stop()

    assert status == 200
    assert payload["status"] == "degraded"
    assert payload["database"] == "error"


async def test_a_failing_snapshot_still_answers_200(bot):
    async def explode() -> dict:
        raise RuntimeError("snapshot exploded")

    port = free_port()
    server = await serve(explode, port)
    try:
        status, payload = await get(port)
    finally:
        await server.stop()

    assert status == 200
    assert payload["status"] == "degraded"


async def test_a_hanging_snapshot_times_out_instead_of_blocking(bot, monkeypatch):
    monkeypatch.setattr("app.health.SNAPSHOT_TIMEOUT_SECONDS", 0.05)

    async def never() -> dict:
        await asyncio.sleep(30)
        return {}

    port = free_port()
    server = await serve(never, port)
    try:
        status, payload = await asyncio.wait_for(get(port), timeout=5)
    finally:
        await server.stop()

    assert status == 200
    assert "timed out" in payload["detail"]


# --------------------------------------------------------------- PORT handling


def test_port_comes_from_the_environment(monkeypatch, database_url):
    """Render injects $PORT; the app must bind exactly that."""
    monkeypatch.setenv("CANVAS_BASE_URL", "https://canvas.example.edu")
    monkeypatch.setenv("CANVAS_API_TOKEN", "token")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "123456789")
    monkeypatch.setenv("DATABASE_URL", database_url)

    monkeypatch.setenv("PORT", "10000")
    assert load_config(env_file=None).http_port == 10000

    monkeypatch.delenv("PORT")
    config = load_config(env_file=None)
    assert config.http_port == 8080, "local default"
    assert config.http_host == "0.0.0.0", "must be reachable from outside the container"


@pytest.mark.parametrize("bad", ["0", "70000", "http"])
def test_invalid_ports_are_rejected(monkeypatch, database_url, bad):
    from app.config import ConfigError

    monkeypatch.setenv("CANVAS_BASE_URL", "https://canvas.example.edu")
    monkeypatch.setenv("CANVAS_API_TOKEN", "token")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "123456789")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("PORT", bad)
    with pytest.raises(ConfigError):
        load_config(env_file=None)


async def test_the_server_listens_on_the_port_it_was_given(bot):
    port = free_port()
    server = await serve(bot.health_snapshot, port)
    try:
        assert server.port == port
        status, _ = await get(port)
        assert status == 200
    finally:
        await server.stop()


# ------------------------------------------------------ startup and shutdown


async def test_a_port_already_in_use_is_a_startup_failure(bot):
    """Refusing to start beats running a bot behind an unreachable endpoint."""
    port = free_port()
    first = await serve(bot.health_snapshot, port)
    second = HealthServer(bot.health_snapshot, host=HOST, port=port)
    try:
        with pytest.raises(HealthServerError):
            await second.start()
    finally:
        await first.stop()


async def test_stopping_releases_the_port(bot):
    port = free_port()
    server = await serve(bot.health_snapshot, port)
    await server.stop()

    # Rebinding proves the listener really went away on shutdown.
    again = await serve(bot.health_snapshot, port)
    try:
        status, _ = await get(port)
        assert status == 200
    finally:
        await again.stop()


async def test_stop_is_idempotent(bot):
    server = await serve(bot.health_snapshot, free_port())
    await server.stop()
    await server.stop()


async def test_the_loops_keep_running_without_any_http_traffic(config, bot):
    """Reminder evaluation must not depend on incoming requests."""
    from tests.conftest import NOW, make_assignment, seed

    sent: list[int] = []

    async def notifier(assignment, threshold, remaining):
        sent.append(threshold)
        return True

    bot.service.notifier = notifier
    seed(bot.service, make_assignment(due_in_minutes=55))

    server = await serve(bot.health_snapshot, free_port())
    try:
        assert await bot.service.evaluate_reminders(now=NOW) == 1
    finally:
        await server.stop()
    assert sent == [60]
