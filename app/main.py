"""Entrypoint: validate config, open PostgreSQL, serve /health, run the loops.

Startup order (see README):
  1. validate configuration        6. hydrate the in-memory cache  ) inside
  2. connect to PostgreSQL         7. verify Canvas authentication ) the bot's
  3. bind the HTTP health server   8. initial Canvas sync          ) setup_hook
  4. connect to Discord            9. sync + reminder loops        )
  5.                              10. register slash commands

After step 6 the loops read cached state, and PostgreSQL is touched only when
something actually changes. See app/cache.py.

Steps 3 and 4 are both prerequisites for a healthy process. The health server
is bound *before* Discord so a port that cannot be claimed fails the deploy
immediately rather than leaving a bot running behind an endpoint nobody can
reach — on a platform that routes by port, that would look alive while being
unreachable.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import discord
import psycopg

from .canvas_client import CanvasClient
from .config import Config, ConfigError, load_config
from .database import Database, DatabaseUnavailable
from .discord_bot import CanvasReminderBot
from .health import HealthServer, HealthServerError

log = logging.getLogger("app")

# Render sends SIGTERM and follows with SIGKILL. Unwind well inside that window
# so the gateway closes politely instead of being shot; a wedged websocket must
# not hold the process open past it.
SHUTDOWN_GRACE_SECONDS = 10.0


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # discord.py is chatty at INFO about gateway internals.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def run(config: Config) -> int:
    try:
        db = Database(config.database_url)
    except (psycopg.Error, DatabaseUnavailable) as exc:
        log.error("Could not connect to DATABASE_URL (%s): %s", config.safe_database_url, exc)
        return 1
    db.prune()

    canvas = CanvasClient(config.canvas_base_url, config.canvas_api_token)
    bot = CanvasReminderBot(config, db, canvas)
    health = HealthServer(
        bot.health_snapshot, host=config.http_host, port=config.http_port
    )

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    bot_task: asyncio.Task[None] | None = None

    async def _shutdown() -> None:
        # SIGTERM is how the platform announces a redeploy. Closing the gateway
        # lets discord.py unwind cleanly instead of being killed mid-write.
        await bot.close()
        if bot_task is None or bot_task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(bot_task), SHUTDOWN_GRACE_SECONDS)
        except (TimeoutError, asyncio.CancelledError):
            log.warning("Gateway did not close within %ss; cancelling", SHUTDOWN_GRACE_SECONDS)
            bot_task.cancel()

    def _request_stop() -> None:
        if not stopping.is_set():
            stopping.set()
            log.info("Shutdown signal received; closing down")
            asyncio.create_task(_shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
            pass

    try:
        await health.start()
    except HealthServerError as exc:
        # Refuse to run half-deployed: a web service that never binds its port
        # is a failed deploy, and a bot behind it would silently be lost.
        log.error("%s", exc)
        await bot.close()
        await canvas.aclose()
        db.close()
        return 1

    exit_code = 0
    bot_task = asyncio.create_task(bot.start(config.discord_bot_token))
    try:
        await bot_task
    except discord.LoginFailure:
        log.error("Discord rejected DISCORD_BOT_TOKEN. Check the token in your environment")
        exit_code = 1
    except discord.PrivilegedIntentsRequired:
        log.error("Discord requires intents this bot did not request; check the bot settings")
        exit_code = 1
    except asyncio.CancelledError:
        pass
    finally:
        await health.stop()
        if not bot.is_closed():
            await bot.close()
        await canvas.aclose()
        db.close()
        log.info("Stopped")
    return exit_code


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        # Logging is not configured yet, and this must never print a token.
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2

    setup_logging(config.log_level)
    log.info(
        "Starting Canvas reminder bot — canvas=%s tz=%s db=%s health=%s:%s",
        config.canvas_base_url,
        config.timezone_name,
        config.safe_database_url,
        config.http_host,
        config.http_port,
    )
    try:
        return asyncio.run(run(config))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
