"""Pagination, retries and auth handling against a mocked HTTP transport."""

from __future__ import annotations

import httpx
import pytest

from app.canvas_client import CanvasAuthError, CanvasClient, CanvasError, parse_next_link


def make_client(handler: httpx.MockTransport) -> CanvasClient:
    http = httpx.AsyncClient(
        transport=handler, headers={"Authorization": "Bearer redacted"}, base_url="https://canvas.example.edu"
    )
    return CanvasClient("https://canvas.example.edu", "redacted", client=http, max_retries=2)


def test_parse_next_link():
    header = (
        '<https://c/api/v1/courses?page=1>; rel="current", '
        '<https://c/api/v1/courses?page=2>; rel="next", '
        '<https://c/api/v1/courses?page=9>; rel="last"'
    )
    assert parse_next_link(header) == "https://c/api/v1/courses?page=2"
    assert parse_next_link(None) is None
    assert parse_next_link('<https://c/x>; rel="last"') is None


async def test_pagination_follows_every_next_link():
    pages = {
        "1": ([{"id": 1}, {"id": 2}], 'rel="next"'),
        "2": ([{"id": 3}], None),
    }
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        seen.append(page)
        body, _ = pages[page]
        headers = {}
        if page == "1":
            headers["Link"] = '<https://canvas.example.edu/api/v1/courses?page=2>; rel="next"'
        return httpx.Response(200, json=body, headers=headers)

    client = make_client(httpx.MockTransport(handler))
    courses = await client.get_active_courses()
    assert [c["id"] for c in courses] == [1, 2, 3]
    assert seen == ["1", "2"]
    await client.aclose()


async def test_per_page_is_requested_so_pagination_is_not_pathological():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json=[])

    client = make_client(httpx.MockTransport(handler))
    await client.get_active_courses()
    assert captured["per_page"] == "100"
    await client.aclose()


async def test_assignments_request_includes_the_submission():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=[])

    client = make_client(httpx.MockTransport(handler))
    await client.get_course_assignments(42)
    assert "/courses/42/assignments" in captured["url"]
    assert "submission" in captured["url"]
    await client.aclose()


async def test_a_bad_token_raises_auth_error_and_is_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"errors": [{"message": "Invalid access token."}]})

    client = make_client(httpx.MockTransport(handler))
    with pytest.raises(CanvasAuthError):
        await client.verify_token()
    assert len(calls) == 1
    await client.aclose()


async def test_transient_server_errors_are_retried_then_succeed(monkeypatch):
    import app.canvas_client as module

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"id": 7, "name": "Student"})

    client = make_client(httpx.MockTransport(handler))
    profile = await client.verify_token()
    assert profile["id"] == 7
    assert len(attempts) == 3
    await client.aclose()


async def test_persistent_failure_raises_canvas_error(monkeypatch):
    import app.canvas_client as module

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = make_client(httpx.MockTransport(handler))
    with pytest.raises(CanvasError):
        await client.get_active_courses()
    await client.aclose()


async def test_network_errors_are_retried_and_then_reported(monkeypatch):
    import app.canvas_client as module

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = make_client(httpx.MockTransport(handler))
    with pytest.raises(CanvasError):
        await client.get_active_courses()
    await client.aclose()


async def test_rate_limit_response_is_retried(monkeypatch):
    import app.canvas_client as module

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    attempts = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(403, text="403 Forbidden (Rate Limit Exceeded)")
        return httpx.Response(200, json=[{"id": 1, "name": "CS 240"}])

    client = make_client(httpx.MockTransport(handler))
    courses = await client.get_active_courses()
    assert courses[0]["id"] == 1
    await client.aclose()
