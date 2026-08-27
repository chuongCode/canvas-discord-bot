from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zoneinfo import ZoneInfo  # noqa: E402

from app.config import Config  # noqa: E402
from app.database import Database  # noqa: E402
from app.models import Assignment  # noqa: E402

NOW = datetime(2025, 9, 4, 12, 0, tzinfo=UTC)

SKIP_REASON = (
    "No PostgreSQL available. Either install the dev extras "
    "(`pip install -r requirements-dev.txt`, which ships an embedded server) "
    "or point TEST_DATABASE_URL at a throwaway database."
)


def _with_schema(dsn: str, schema: str) -> str:
    """Return ``dsn`` pinned to one schema via the libpq ``options`` parameter."""
    parts = urlsplit(dsn)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "options"]
    query.append(("options", f"-csearch_path={schema}"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    """A libpq URL for a PostgreSQL server the tests may freely write to.

    Prefers an explicitly provided database; otherwise starts the embedded
    server that ships with the dev requirements. Skips cleanly when neither is
    available, rather than pretending to have tested persistence.
    """
    explicit = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if explicit:
        return explicit

    try:
        import pgserver
    except ImportError:  # pragma: no cover - depends on the environment
        pytest.skip(SKIP_REASON, allow_module_level=True)

    import tempfile

    data_dir = Path(tempfile.mkdtemp(prefix="phat-pg-")) / "pgdata"
    try:
        server = pgserver.get_server(data_dir, cleanup_mode="stop")
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"{SKIP_REASON} ({exc})", allow_module_level=True)
    return server.get_uri()


@pytest.fixture
def database_url(postgres_dsn) -> str:
    """An isolated, empty schema for one test, dropped afterwards.

    A schema rather than a whole database because reopening it is what the
    restart tests do, and it is an order of magnitude faster to create.
    """
    import psycopg

    schema = f"t_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(postgres_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield _with_schema(postgres_dsn, schema)
    finally:
        with psycopg.connect(postgres_dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture
def db(database_url) -> Database:
    database = Database(database_url)
    yield database
    database.close()


@pytest.fixture
def config(database_url) -> Config:
    return Config(
        canvas_base_url="https://canvas.example.edu",
        canvas_api_token="not-a-real-token",
        discord_bot_token="not-a-real-token",
        discord_channel_id=1234,
        timezone_name="America/Chicago",
        tz=ZoneInfo("America/Chicago"),
        database_url=database_url,
    )


def seed(service, assignment, *, seen_at=None):
    """Persist an assignment and mirror it into the cache, as a sync would.

    Tests that put rows straight into PostgreSQL have to do this now: the
    reminder loop reads the cache, so a write the cache never heard about is
    invisible until the next hydrate or sync — which is exactly the production
    behaviour, not a test artefact.
    """
    service.db.upsert_assignment(assignment, seen_at=seen_at)
    service.cache.put(service.db.get_assignment(assignment.id))


def make_assignment(
    *,
    assignment_id: int = 123,
    due_in_minutes: float | None = 60,
    now: datetime = NOW,
    is_complete: bool = False,
    submission_known: bool = True,
    submission_state: str = "unsubmitted",
    name: str = "Project 3",
    course_name: str = "CS 240",
    course_id: int = 1,
) -> Assignment:
    due_at = None if due_in_minutes is None else now + timedelta(minutes=due_in_minutes)
    return Assignment(
        id=assignment_id,
        course_id=course_id,
        course_name=course_name,
        name=name,
        due_at=due_at,
        html_url=f"https://canvas.example.edu/courses/{course_id}/assignments/{assignment_id}",
        submission_state=submission_state,
        is_complete=is_complete,
        submission_known=submission_known,
    )
