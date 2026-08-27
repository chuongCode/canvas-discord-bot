"""Thin async Canvas LMS REST client.

Only what this bot needs: verify the token, list active courses, list each
course's assignments with the caller's own submission attached. Handles Link
header pagination, 429/5xx backoff and transport errors.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

log = logging.getLogger(__name__)

_LINK_RE = re.compile(r'<(?P<url>[^>]+)>\s*;\s*rel="(?P<rel>[^"]+)"')

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class CanvasError(RuntimeError):
    """Any Canvas failure the caller should treat as 'no fresh data'."""


class CanvasAuthError(CanvasError):
    """Token missing, expired or lacking scope. Retrying will not help."""


def parse_next_link(link_header: str | None) -> str | None:
    """Extract the rel="next" URL from a Canvas Link header."""
    if not link_header:
        return None
    for match in _LINK_RE.finditer(link_header):
        if match.group("rel").strip().lower() == "next":
            return match.group("url").strip()
    return None


class CanvasClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 4,
        per_page: int = 100,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_root = f"{self.base_url}/api/v1"
        self._token = token
        self.max_retries = max_retries
        self.per_page = per_page
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "phat-discord-bot/1.0 (+personal canvas reminder bot)",
            },
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> CanvasClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ----------------------------------------------------------- transport

    async def _request(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET with bounded exponential backoff. Never logs the token."""
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                delay = self._backoff(attempt)
                log.warning(
                    "Canvas request failed (%s: %s); retrying in %.1fs",
                    type(exc).__name__, exc, delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code in (401, 403):
                # 403 is also Canvas's rate-limit signal ("Rate Limit Exceeded").
                if "rate limit" in response.text.lower():
                    if attempt >= self.max_retries:
                        raise CanvasError("Canvas rate limit exceeded")
                    delay = self._backoff(attempt, response)
                    log.warning("Canvas rate limited; sleeping %.1fs", delay)
                    await asyncio.sleep(delay)
                    continue
                raise CanvasAuthError(
                    f"Canvas rejected the API token (HTTP {response.status_code})"
                )

            if response.status_code in RETRY_STATUS:
                last_error = CanvasError(f"Canvas returned HTTP {response.status_code}")
                if attempt >= self.max_retries:
                    break
                delay = self._backoff(attempt, response)
                log.warning(
                    "Canvas HTTP %s from %s; retrying in %.1fs",
                    response.status_code, _safe_url(url), delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                raise CanvasError(
                    f"Canvas returned HTTP {response.status_code} for {_safe_url(url)}"
                )

            return response

        raise CanvasError(f"Canvas request to {_safe_url(url)} failed: {last_error}")

    def _backoff(self, attempt: int, response: httpx.Response | None = None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(60.0, max(1.0, float(retry_after)))
                except ValueError:
                    pass
        return min(30.0, (2**attempt)) + random.uniform(0, 1.0)

    async def _paginate(self, path: str, params: dict[str, Any] | None = None) -> AsyncIterator[dict]:
        """Yield every item across all pages, following rel="next" links."""
        url = path if path.startswith("http") else f"{self.api_root}{path}"
        query: dict[str, Any] | None = {"per_page": self.per_page, **(params or {})}
        pages = 0
        while url:
            response = await self._request(url, query)
            try:
                payload = response.json()
            except ValueError as exc:
                raise CanvasError(f"Canvas returned non-JSON for {_safe_url(url)}") from exc

            if isinstance(payload, dict) and "errors" in payload:
                raise CanvasError(f"Canvas error: {payload.get('errors')}")
            if not isinstance(payload, list):
                raise CanvasError(f"Expected a list from {_safe_url(url)}")

            for item in payload:
                if isinstance(item, dict):
                    yield item

            pages += 1
            if pages >= 100:  # safety valve against a pagination loop
                log.warning("Stopping pagination for %s after %d pages", _safe_url(url), pages)
                break

            next_url = parse_next_link(response.headers.get("Link"))
            if not next_url:
                break
            # The next link already carries the query string.
            url, query = next_url, None

    # --------------------------------------------------------------- API

    async def verify_token(self) -> dict[str, Any]:
        """Confirm the token works. Returns the Canvas user profile."""
        response = await self._request(f"{self.api_root}/users/self/profile")
        try:
            profile = response.json()
        except ValueError as exc:
            raise CanvasError("Canvas profile response was not JSON") from exc
        if not isinstance(profile, dict):
            raise CanvasError("Canvas profile response was malformed")
        return profile

    async def get_active_courses(self, *, students_only: bool = True) -> list[dict]:
        """Currently available courses with an active enrollment."""
        params: dict[str, Any] = {
            "enrollment_state": "active",
            "state[]": "available",
            "include[]": "term",
        }
        if students_only:
            params["enrollment_type"] = "student"
        return [course async for course in self._paginate("/courses", params)]

    async def get_course_assignments(self, course_id: int) -> list[dict]:
        """Assignments for a course, each with the caller's submission inlined.

        `include[]=submission` is what makes submission awareness cheap: one
        request per course instead of one per assignment.
        """
        params = {
            "include[]": ["submission", "all_dates"],
            "order_by": "due_at",
        }
        return [item async for item in self._paginate(f"/courses/{course_id}/assignments", params)]


def _safe_url(url: str) -> str:
    """Strip any query string before logging so tokens can never leak."""
    return url.split("?", 1)[0]
