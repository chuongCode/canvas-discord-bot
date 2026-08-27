"""Configuration loaded from environment variables.

Every secret and tunable lives here. Nothing else in the app reads os.environ.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# Reminder thresholds, in minutes before the due date. This is the one place to
# change them (or override with REMINDER_THRESHOLDS_MINUTES).
DEFAULT_THRESHOLDS_MINUTES: tuple[int, ...] = (
    720,  # 12 hours
    300,  # 5 hours
    120,  # 2 hours
    60,  # 1 hour
    30,
    15,
    10,
    5,
    1,
)


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


@dataclass(frozen=True)
class Config:
    canvas_base_url: str
    canvas_api_token: str
    discord_bot_token: str
    discord_channel_id: int
    discord_guild_id: int | None = None

    timezone_name: str = "America/Chicago"

    # libpq connection URI. PostgreSQL is the only supported store: the app is
    # designed to run on hosts with an ephemeral filesystem.
    database_url: str = ""

    # The health server. Render injects PORT; locally it defaults to 8080.
    http_host: str = "0.0.0.0"
    http_port: int = 8080

    canvas_sync_interval_seconds: int = 300
    reminder_interval_seconds: int = 45

    thresholds_minutes: tuple[int, ...] = DEFAULT_THRESHOLDS_MINUTES
    monitor_lookahead_days: int = 30
    monitor_past_grace_hours: int = 24

    student_enrollments_only: bool = True
    suppress_reminders_on_first_run: bool = False

    log_level: str = "INFO"

    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("America/Chicago"))

    @property
    def canvas_api_root(self) -> str:
        return f"{self.canvas_base_url}/api/v1"

    @property
    def safe_database_url(self) -> str:
        """The database URL with any password redacted, safe to log."""
        return redact_database_url(self.database_url)


# libpq understands both spellings; SQLAlchemy-style "+driver" suffixes and the
# legacy Heroku/Render "postgres://" alias are normalised so a URL copied from
# any dashboard just works.
_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})


def _normalize_database_url(raw: str) -> str:
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    base_scheme = scheme.split("+", 1)[0]
    if base_scheme not in _POSTGRES_SCHEMES:
        if base_scheme in {"sqlite", "file"} or not base_scheme:
            raise ConfigError(
                "DATABASE_URL must be a PostgreSQL URL "
                "(postgresql://user:password@host:port/dbname). "
                "SQLite is no longer supported: this app is built for hosts "
                "with an ephemeral filesystem."
            )
        raise ConfigError(f"DATABASE_URL has unsupported scheme {parts.scheme!r}; expected postgresql://")
    if not parts.netloc and not parts.path:
        raise ConfigError("DATABASE_URL is missing a host and database name")
    return urlunsplit(("postgresql", parts.netloc, parts.path, parts.query, parts.fragment))


def redact_database_url(url: str) -> str:
    """Strip the password out of a libpq URL so it can safely be logged."""
    if not url:
        return "<unset>"
    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover - urlsplit is very permissive
        return "<unparseable>"
    if not parts.hostname:
        # A Unix-socket URL carries its path in the query string; drop it
        # wholesale rather than trying to decide which parts are sensitive.
        return urlunsplit((parts.scheme, "", parts.path, "", "")) or url
    userinfo = ""
    if parts.username:
        userinfo = parts.username + (":***" if parts.password else "") + "@"
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, f"{userinfo}{host}", parts.path, "", ""))


def _require(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:  # pragma: no cover - trivial
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _thresholds() -> tuple[int, ...]:
    raw = (os.environ.get("REMINDER_THRESHOLDS_MINUTES") or "").strip()
    if not raw:
        return DEFAULT_THRESHOLDS_MINUTES
    try:
        values = {int(part.strip()) for part in raw.split(",") if part.strip()}
    except ValueError as exc:
        raise ConfigError(
            f"REMINDER_THRESHOLDS_MINUTES must be a comma-separated list of "
            f"integers, got {raw!r}"
        ) from exc
    if not values or any(v <= 0 for v in values):
        raise ConfigError("REMINDER_THRESHOLDS_MINUTES must contain positive integers")
    return tuple(sorted(values, reverse=True))


def load_config(*, env_file: str | os.PathLike[str] | None = ".env") -> Config:
    """Read and validate configuration. Raises ConfigError on bad input."""
    if env_file is not None and Path(env_file).is_file():
        load_dotenv(env_file, override=False)

    base_url = _require("CANVAS_BASE_URL").rstrip("/")
    if base_url.endswith("/api/v1"):
        base_url = base_url[: -len("/api/v1")]
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError("CANVAS_BASE_URL must start with http:// or https://")

    channel_raw = _require("DISCORD_CHANNEL_ID")
    try:
        channel_id = int(channel_raw)
    except ValueError as exc:
        raise ConfigError("DISCORD_CHANNEL_ID must be a numeric Discord ID") from exc

    guild_raw = (os.environ.get("DISCORD_GUILD_ID") or "").strip()
    guild_id: int | None = None
    if guild_raw:
        try:
            guild_id = int(guild_raw)
        except ValueError as exc:
            raise ConfigError("DISCORD_GUILD_ID must be a numeric Discord ID") from exc

    tz_name = (os.environ.get("TIMEZONE") or "America/Chicago").strip()
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"TIMEZONE {tz_name!r} is not a valid IANA timezone") from exc

    return Config(
        canvas_base_url=base_url,
        canvas_api_token=_require("CANVAS_API_TOKEN"),
        discord_bot_token=_require("DISCORD_BOT_TOKEN"),
        discord_channel_id=channel_id,
        discord_guild_id=guild_id,
        timezone_name=tz_name,
        tz=tz,
        database_url=_normalize_database_url(_require("DATABASE_URL")),
        http_host=(os.environ.get("HTTP_HOST") or "0.0.0.0").strip(),
        # Render injects PORT and routes external traffic to it.
        http_port=_int("PORT", 8080, minimum=1, maximum=65535),
        canvas_sync_interval_seconds=_int("CANVAS_SYNC_INTERVAL_SECONDS", 300, minimum=30),
        reminder_interval_seconds=_int("REMINDER_INTERVAL_SECONDS", 45, minimum=5),
        thresholds_minutes=_thresholds(),
        monitor_lookahead_days=_int("MONITOR_LOOKAHEAD_DAYS", 30),
        monitor_past_grace_hours=_int("MONITOR_PAST_GRACE_HOURS", 24, minimum=0),
        student_enrollments_only=_bool("CANVAS_STUDENT_ENROLLMENTS_ONLY", True),
        suppress_reminders_on_first_run=_bool("SUPPRESS_REMINDERS_ON_FIRST_RUN", False),
        log_level=(os.environ.get("LOG_LEVEL") or "INFO").strip().upper(),
    )
