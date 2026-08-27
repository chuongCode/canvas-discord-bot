"""A tiny HTTP health endpoint that runs alongside the Discord client.

Why this exists: on a free-tier host the web service is put to sleep unless
something keeps talking to it, and the platform itself wants a URL to probe.
An external uptime monitor calls ``GET /health`` on a schedule. The bot does
*not* ping itself — self-pinging is both pointless (a sleeping process cannot
wake itself) and abusive.

``aiohttp`` is already a dependency of discord.py, so this adds no new package.
It shares the bot's event loop: the sync and reminder loops keep running
whether or not requests arrive, and a request never touches Canvas.

Status codes are deliberate. ``/health`` answers 200 for as long as the process
is alive and its loops are scheduled, and reports Canvas/Discord/database
trouble in the JSON body instead. A transient Canvas outage must not read as
"kill this container" to the platform, nor make an uptime monitor page you.
Only a bot that has actually shut down answers 503.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)

# The snapshot is built off the event loop, so it may block briefly on the
# database; a slow query must not stall Discord's heartbeat forever.
SNAPSHOT_TIMEOUT_SECONDS = 5.0

SnapshotProvider = Callable[[], Awaitable[dict[str, Any]]]


class HealthServerError(RuntimeError):
    """Raised when the HTTP server cannot be bound. Startup must not continue."""


def _json(payload: dict[str, Any], status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        # allow_nan=False: an unconnected gateway reports NaN latency, and NaN
        # is not valid JSON.
        text=json.dumps(payload, allow_nan=False, default=str),
        content_type="application/json",
    )


class HealthServer:
    """Serves ``GET /health`` (and ``GET /`` as an alias) on host:port."""

    def __init__(self, snapshot: SnapshotProvider, *, host: str, port: int) -> None:
        self._snapshot = snapshot
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None

    def build_app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/health", self.handle_health),
                web.get("/healthz", self.handle_health),
                # Uptime monitors and platform probes often hit the root.
                web.get("/", self.handle_health),
            ]
        )
        return app

    async def handle_health(self, _request: web.Request) -> web.Response:
        try:
            payload = await asyncio.wait_for(self._snapshot(), SNAPSHOT_TIMEOUT_SECONDS)
        except TimeoutError:
            log.warning("Health snapshot timed out after %ss", SNAPSHOT_TIMEOUT_SECONDS)
            return _json({"status": "degraded", "detail": "health snapshot timed out"})
        except Exception:
            log.exception("Health snapshot failed")
            return _json({"status": "degraded", "detail": "health snapshot failed"})

        status = 503 if payload.get("status") == "stopped" else 200
        return _json(payload, status)

    async def start(self) -> None:
        """Bind the listener. Raises :class:`HealthServerError` if it cannot."""
        runner = web.AppRunner(self.build_app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port, shutdown_timeout=5.0)
        try:
            await site.start()
        except OSError as exc:
            await runner.cleanup()
            raise HealthServerError(
                f"Could not bind the health server to {self.host}:{self.port}: {exc}"
            ) from exc
        self._runner = runner
        log.info("Health endpoint listening on http://%s:%s/health", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            log.info("Health endpoint stopped")
