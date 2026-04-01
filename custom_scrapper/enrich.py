from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .serp import SerpResult, google_serp_search_detailed


class SerpCache:
    def __init__(self, path: str = "output/serp_cache.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def get(self, key: str) -> Any | None:
        return self._data.get(key)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")


def _first_non_wellfound(results: list[SerpResult]) -> str | None:
    for r in results:
        u = r.url
        if "wellfound.com" in u:
            continue
        return u
    return None


def discover_company_website(company: str, *, cache: SerpCache, sleep_s: float = 0.0) -> str | None:
    if not company:
        return None
    q = f"{company} official website"
    cached = cache.get(f"website::{q}")
    if cached is not None:
        return cached

    res = google_serp_search_detailed(q, max_results=5, sleep_s=sleep_s, wellfound_jobs_only=False)
    url = _first_non_wellfound(res)
    cache.set(f"website::{q}", url)
    return url


def discover_contact_or_apply_links(
    *,
    company: str | None,
    role: str | None,
    cache: SerpCache,
    sleep_s: float = 0.0,
) -> dict[str, str | None]:
    if not company:
        return {"apply_url": None, "contact_url": None, "google_form_url": None}

    company_q = company
    role_q = role or ""

    ats_query = (
        f"{company_q} {role_q} apply "
        "(site:lever.co OR site:jobs.lever.co OR site:greenhouse.io OR site:boards.greenhouse.io "
        "OR site:ashbyhq.com OR site:workable.com OR site:smartrecruiters.com OR site:breezy.hr)"
    )
    cached = cache.get(f"apply::{ats_query}")
    if cached is None:
        res = google_serp_search_detailed(ats_query, max_results=5, sleep_s=sleep_s, wellfound_jobs_only=False)
        apply_url = _first_non_wellfound(res)
        cache.set(f"apply::{ats_query}", apply_url)
    else:
        apply_url = cached

    contact_query = f"{company_q} contact us"
    cached = cache.get(f"contact::{contact_query}")
    if cached is None:
        res = google_serp_search_detailed(contact_query, max_results=5, sleep_s=sleep_s, wellfound_jobs_only=False)
        contact_url = _first_non_wellfound(res)
        cache.set(f"contact::{contact_query}", contact_url)
    else:
        contact_url = cached

    gform_query = f"{company_q} (site:docs.google.com/forms OR site:forms.gle)"
    cached = cache.get(f"gform::{gform_query}")
    if cached is None:
        res = google_serp_search_detailed(gform_query, max_results=5, sleep_s=sleep_s, wellfound_jobs_only=False)
        google_form_url = _first_non_wellfound(res)
        cache.set(f"gform::{gform_query}", google_form_url)
    else:
        google_form_url = cached

    return {"apply_url": apply_url, "contact_url": contact_url, "google_form_url": google_form_url}
