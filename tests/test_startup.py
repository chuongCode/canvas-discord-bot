"""The whole app must construct and initialise with placeholder credentials.

This is the check that catches import cycles, bad decorators and malformed
slash-command definitions without ever touching the network.
"""

from __future__ import annotations

import pytest

from app.canvas_client import CanvasClient
from app.config import load_config
from app.database import Database
from app.discord_bot import CanvasReminderBot

PLACEHOLDER_ENV = {
    "CANVAS_BASE_URL": "https://canvas.example.edu",
    "CANVAS_API_TOKEN": "placeholder-not-a-real-token",
    "DISCORD_BOT_TOKEN": "placeholder-not-a-real-token",
    "DISCORD_CHANNEL_ID": "111222333444555666",
    "TIMEZONE": "America/Chicago",
}


@pytest.fixture
def placeholder_config(monkeypatch, database_url):
    for key, value in PLACEHOLDER_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.delenv("PORT", raising=False)
    return load_config(env_file=None)


def test_app_initialises_end_to_end_without_real_credentials(placeholder_config):
    db = Database(placeholder_config.database_url)
    canvas = CanvasClient(placeholder_config.canvas_base_url, placeholder_config.canvas_api_token)
    bot = CanvasReminderBot(placeholder_config, db, canvas)

    assert placeholder_config.database_url.startswith("postgresql://")
    assert bot.service is not None
    assert bot.sync_loop.seconds == placeholder_config.canvas_sync_interval_seconds
    assert bot.reminder_loop.seconds == placeholder_config.reminder_interval_seconds
    db.close()


def test_every_documented_slash_command_registers(placeholder_config):
    db = Database(placeholder_config.database_url)
    canvas = CanvasClient(placeholder_config.canvas_base_url, placeholder_config.canvas_api_token)
    bot = CanvasReminderBot(placeholder_config, db, canvas)
    bot._register_commands()

    names = {command.name for command in bot.tree.get_commands()}
    assert {"due", "today", "week", "sync", "status"} <= names
    db.close()


def test_sync_command_is_owner_restricted(placeholder_config):
    db = Database(placeholder_config.database_url)
    canvas = CanvasClient(placeholder_config.canvas_base_url, placeholder_config.canvas_api_token)
    bot = CanvasReminderBot(placeholder_config, db, canvas)
    bot._register_commands()

    sync_command = {c.name: c for c in bot.tree.get_commands()}["sync"]
    assert sync_command.checks, "/sync must carry an authorisation check"
    db.close()


def test_bot_requests_only_the_guilds_intent(placeholder_config):
    db = Database(placeholder_config.database_url)
    canvas = CanvasClient(placeholder_config.canvas_base_url, placeholder_config.canvas_api_token)
    bot = CanvasReminderBot(placeholder_config, db, canvas)

    assert bot.intents.guilds is True
    assert bot.intents.message_content is False
    assert bot.intents.members is False
    assert bot.intents.presences is False
    db.close()


def test_repository_never_ships_a_committed_env_file():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    gitignore = (root / ".gitignore").read_text()
    assert ".env" in gitignore
    assert (root / ".env.example").exists()
