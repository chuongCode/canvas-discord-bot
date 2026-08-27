"""Configuration validation — the app must refuse to start on bad input."""

from __future__ import annotations

import pytest

from app.config import DEFAULT_THRESHOLDS_MINUTES, ConfigError, load_config

BASE_ENV = {
    "CANVAS_BASE_URL": "https://canvas.example.edu",
    "CANVAS_API_TOKEN": "token",
    "DISCORD_BOT_TOKEN": "token",
    "DISCORD_CHANNEL_ID": "123456789",
    "DATABASE_URL": "postgresql://bot:secret@db.example.com:5432/phat",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(BASE_ENV) + [
        "DISCORD_GUILD_ID", "TIMEZONE", "DATABASE_URL", "PORT", "HTTP_HOST",
        "REMINDER_THRESHOLDS_MINUTES",
        "CANVAS_SYNC_INTERVAL_SECONDS", "REMINDER_INTERVAL_SECONDS", "MONITOR_LOOKAHEAD_DAYS",
        "MONITOR_PAST_GRACE_HOURS", "CANVAS_STUDENT_ENROLLMENTS_ONLY",
        "SUPPRESS_REMINDERS_ON_FIRST_RUN", "LOG_LEVEL",
    ]:
        monkeypatch.delenv(key, raising=False)


def setenv(monkeypatch, **overrides):
    for key, value in {**BASE_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)


def test_valid_config_uses_documented_defaults(monkeypatch):
    setenv(monkeypatch)
    config = load_config(env_file=None)
    assert config.timezone_name == "America/Chicago"
    assert config.thresholds_minutes == DEFAULT_THRESHOLDS_MINUTES
    assert config.canvas_api_root == "https://canvas.example.edu/api/v1"


@pytest.mark.parametrize("missing", list(BASE_ENV))
def test_missing_required_variables_are_rejected(monkeypatch, missing):
    setenv(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(ConfigError) as exc:
        load_config(env_file=None)
    assert missing in str(exc.value)


def test_api_suffix_in_base_url_is_tolerated(monkeypatch):
    setenv(monkeypatch, CANVAS_BASE_URL="https://canvas.example.edu/api/v1/")
    assert load_config(env_file=None).canvas_base_url == "https://canvas.example.edu"


def test_bad_timezone_is_rejected(monkeypatch):
    setenv(monkeypatch, TIMEZONE="Mars/Olympus_Mons")
    with pytest.raises(ConfigError):
        load_config(env_file=None)


def test_non_numeric_channel_id_is_rejected(monkeypatch):
    setenv(monkeypatch, DISCORD_CHANNEL_ID="general")
    with pytest.raises(ConfigError):
        load_config(env_file=None)


def test_custom_thresholds_are_sorted_descending_and_deduped(monkeypatch):
    setenv(monkeypatch, REMINDER_THRESHOLDS_MINUTES="15, 60, 15, 5")
    assert load_config(env_file=None).thresholds_minutes == (60, 15, 5)


@pytest.mark.parametrize("bad", ["0,10", "-5", "soon"])
def test_invalid_thresholds_are_rejected(monkeypatch, bad):
    setenv(monkeypatch, REMINDER_THRESHOLDS_MINUTES=bad)
    with pytest.raises(ConfigError):
        load_config(env_file=None)


def test_intervals_have_sane_floors(monkeypatch):
    setenv(monkeypatch, CANVAS_SYNC_INTERVAL_SECONDS="5")
    with pytest.raises(ConfigError):
        load_config(env_file=None)


# ------------------------------------------------------------- DATABASE_URL


def test_database_url_is_required(monkeypatch):
    setenv(monkeypatch)
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(ConfigError) as exc:
        load_config(env_file=None)
    assert "DATABASE_URL" in str(exc.value)


@pytest.mark.parametrize(
    "given",
    [
        "postgres://bot:secret@db.example.com:5432/phat",
        "postgresql+psycopg://bot:secret@db.example.com:5432/phat",
    ],
)
def test_alternative_postgres_spellings_are_normalised(monkeypatch, given):
    """Dashboards hand out several spellings; all mean the same thing."""
    setenv(monkeypatch, DATABASE_URL=given)
    url = load_config(env_file=None).database_url
    assert url == "postgresql://bot:secret@db.example.com:5432/phat"


def test_a_sqlite_url_is_rejected_with_an_explanation(monkeypatch):
    setenv(monkeypatch, DATABASE_URL="sqlite:///data/bot.db")
    with pytest.raises(ConfigError) as exc:
        load_config(env_file=None)
    assert "PostgreSQL" in str(exc.value)


def test_a_bare_file_path_is_rejected(monkeypatch):
    setenv(monkeypatch, DATABASE_URL="data/bot.db")
    with pytest.raises(ConfigError):
        load_config(env_file=None)


def test_an_unknown_scheme_is_rejected(monkeypatch):
    setenv(monkeypatch, DATABASE_URL="mysql://bot@db.example.com/phat")
    with pytest.raises(ConfigError):
        load_config(env_file=None)


def test_the_database_password_is_never_exposed_for_logging(monkeypatch):
    setenv(monkeypatch)
    config = load_config(env_file=None)
    safe = config.safe_database_url
    assert "secret" not in safe
    assert "db.example.com" in safe
    assert safe == "postgresql://bot:***@db.example.com:5432/phat"


def test_redacting_a_url_without_a_password_keeps_it_readable():
    from app.config import redact_database_url

    assert redact_database_url("postgresql://db.internal:5432/phat") == (
        "postgresql://db.internal:5432/phat"
    )
    assert redact_database_url("") == "<unset>"


def test_redacting_a_unix_socket_url_drops_the_query_string():
    from app.config import redact_database_url

    redacted = redact_database_url("postgresql://postgres:@/postgres?host=/tmp/pg&password=x")
    assert "password" not in redacted
    assert redacted == "postgresql:/postgres"
