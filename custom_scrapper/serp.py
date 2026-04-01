from __future__ import annotations

import os
import time
from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urlparse

import requests


class SerpApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class SerpResult:
    url: str
    title: str | None = None
    snippet: str | None = None


def google_serp_search_detailed(
    query: str,
    *,
    max_results: int = 20,
    page_size: int = 10,
    sleep_s: float = 0.0,
    wellfound_jobs_only: bool = True,
) -> list[SerpResult]:
    """Query SerpAPI's Google engine and return organic results.

    When `max_results` > `page_size`, this will page using the `start` parameter.
    If `wellfound_jobs_only=True`, it filters to Wellfound job listing URLs.
    """

    api_key = os.getenv("SERP_API_KEY")
    if not api_key:
        raise SerpApiError("Missing SERP_API_KEY in environment (.env)")

    if max_results <= 0:
        return []

    page_size = max(1, min(int(page_size), 100))

    def allow_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False

        if not parsed.scheme.startswith("http"):
            return False

        if not wellfound_jobs_only:
            return True

        netloc = (parsed.netloc or "").lower()
        if not netloc.endswith("wellfound.com"):
            return False

        path = parsed.path or ""
        if not path.startswith("/jobs/"):
            return False
        # Typical job URLs: /jobs/4019760-...
        return re.match(r"^/jobs/\d+", path) is not None

    results: list[SerpResult] = []
    seen: set[str] = set()

    start = 0
    while len(results) < max_results and start < 1000:
        params: dict[str, Any] = {
            "engine": "google",
            "q": query,
            "api_key": api_key,
            "num": min(page_size, max_results - len(results)),
            "start": start,
            "hl": "en",
            "gl": "us",
        }

        resp = requests.get("https://serpapi.com/search.json", params=params, timeout=60)
        if resp.status_code != 200:
            raise SerpApiError(f"SerpAPI HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        organic = data.get("organic_results") or []
        if not organic:
            break

        added_this_page = 0
        for item in organic:
            link = item.get("link")
            if not link or not isinstance(link, str):
                continue
            if not allow_url(link):
                continue
            if link in seen:
                continue
            seen.add(link)

            title = item.get("title") if isinstance(item.get("title"), str) else None
            snippet = item.get("snippet") if isinstance(item.get("snippet"), str) else None
            results.append(SerpResult(url=link, title=title, snippet=snippet))
            added_this_page += 1
            if len(results) >= max_results:
                break

        if sleep_s:
            time.sleep(sleep_s)

        # If nothing new was added, paging more is unlikely to help.
        if added_this_page == 0:
            break

        start += page_size

    return results


def google_serp_search_detailed_filtered(
    query: str,
    *,
    max_results: int = 20,
    page_size: int = 10,
    sleep_s: float = 0.0,
    wellfound_jobs_only: bool = True,
) -> list[SerpResult]:
    """Backwards-compatible alias used by enrichment helpers."""

    return google_serp_search_detailed(
        query,
        max_results=max_results,
        page_size=page_size,
        sleep_s=sleep_s,
        wellfound_jobs_only=wellfound_jobs_only,
    )


def google_serp_search_detailed_paged(
    query: str,
    *,
    max_results: int = 120,
    page_size: int = 10,
    sleep_s: float = 0.0,
) -> list[SerpResult]:
    """Convenience wrapper for paging Wellfound job results."""

    return google_serp_search_detailed(
        query,
        max_results=max_results,
        page_size=page_size,
        sleep_s=sleep_s,
        wellfound_jobs_only=True,
    )


def google_serp_search(query: str, *, max_results: int = 20, sleep_s: float = 0.0) -> list[str]:
    return [
        r.url
        for r in google_serp_search_detailed(
            query,
            max_results=max_results,
            sleep_s=sleep_s,
            wellfound_jobs_only=True,
        )
    ]
