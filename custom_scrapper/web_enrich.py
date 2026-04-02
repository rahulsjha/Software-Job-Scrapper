from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


_DEFAULT_TLDS: tuple[str, ...] = (
    ".com",
    ".ai",
    ".io",
    ".co",
    ".app",
    ".net",
    ".org",
)


@dataclass(frozen=True)
class WebEnrichConfig:
    timeout_s: float = 8.0
    max_pages: int = 4
    sleep_s: float = 0.5
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )


def _company_tokens(company: str) -> list[str]:
    c = (company or "").strip().lower()
    c = re.sub(r"\b(inc|inc\.|llc|ltd|ltd\.|corp|corporation|co|company|gmbh|plc)\b", " ", c)
    c = re.sub(r"[^a-z0-9]+", " ", c)
    toks = [t for t in c.split() if len(t) >= 2]
    return toks[:4]


def _slugify_company(company: str) -> str:
    c = (company or "").strip().lower()
    c = re.sub(r"\b(inc|inc\.|llc|ltd|ltd\.|corp|corporation|co|company|gmbh|plc)\b", " ", c)
    c = re.sub(r"[^a-z0-9]+", "-", c)
    c = re.sub(r"-+", "-", c).strip("-")
    return c


def _session(cfg: WebEnrichConfig) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": cfg.user_agent, "Accept-Language": "en-US,en;q=0.9"})
    return s


def _is_probably_html(resp: requests.Response) -> bool:
    ctype = (resp.headers.get("content-type") or "").lower()
    return "text/html" in ctype or "application/xhtml" in ctype or ctype == ""


def _fetch_html(s: requests.Session, url: str, *, timeout_s: float) -> str | None:
    try:
        resp = s.get(url, timeout=timeout_s, allow_redirects=True)
    except Exception:
        return None
    if resp.status_code >= 400:
        return None
    if not _is_probably_html(resp):
        return None
    text = resp.text or ""
    if len(text) < 200:
        return None
    return text


def _looks_like_company_site(html: str, company: str) -> bool:
    toks = _company_tokens(company)
    if not toks:
        return True
    lower = html.lower()
    hits = sum(1 for t in toks if t in lower)
    return hits >= 1


def guess_company_website(company: str, *, cfg: WebEnrichConfig) -> str | None:
    """Best-effort: guess a company website by trying common domains.

    This does NOT use any external search API; it only performs direct HTTP requests
    to candidate domains.
    """

    slug = _slugify_company(company)
    if not slug:
        return None

    candidates: list[str] = []
    for tld in _DEFAULT_TLDS:
        candidates.append(f"https://{slug}{tld}")
        candidates.append(f"https://www.{slug}{tld}")

    s = _session(cfg)
    for base in candidates:
        html = _fetch_html(s, base, timeout_s=cfg.timeout_s)
        if not html:
            continue
        if not _looks_like_company_site(html, company):
            continue
        return base
    return None


_TEAM_HINTS = (
    "/team",
    "/about",
    "/company",
    "/leadership",
    "/people",
    "/contact",
)


def _same_site(base: str, url: str) -> bool:
    try:
        a = urlparse(base)
        b = urlparse(url)
    except Exception:
        return False
    if not b.netloc:
        return True
    return a.netloc.lower() == b.netloc.lower()


def _candidate_info_pages(base_url: str, soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href or not isinstance(href, str):
            continue
        href = href.strip()
        if href.startswith("mailto:") or href.startswith("tel:"):
            continue
        full = urljoin(base_url, href)
        if not _same_site(base_url, full):
            continue
        path = (urlparse(full).path or "").lower()
        if any(h in path for h in _TEAM_HINTS):
            out.append(full)
    # De-dupe, keep deterministic
    dedup: list[str] = []
    seen: set[str] = set()
    for u in out:
        if u in seen:
            continue
        seen.add(u)
        dedup.append(u)
    return dedup[:10]


_NAME_RE = re.compile(r"^[A-Z][a-zA-Z'\-]+\s+[A-Z][a-zA-Z'\-]+$")


def _extract_names_from_soup(soup: BeautifulSoup) -> list[str]:
    names: list[str] = []
    for el in soup.select("h1,h2,h3,h4,a,span,strong,div"):
        t = (el.get_text(" ", strip=True) or "").strip()
        t = " ".join(t.split())
        if len(t) < 5 or len(t) > 60:
            continue
        if _NAME_RE.match(t):
            names.append(t)

    exclude = {
        "wellfound",
        "meet your team",
        "privacy policy",
        "terms of service",
        "contact us",
        "sign in",
        "log in",
        "login",
    }

    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        ln = n.lower()
        if ln in exclude:
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out[:25]


def discover_team_members(website: str, company: str, *, cfg: WebEnrichConfig) -> list[str]:
    """Find potential team member names from the company website.

    Strategy:
    - Fetch homepage
    - Follow a few internal links that look like /team, /about, /leadership
    - Extract human-like names via a simple heuristic
    """

    if not website:
        return []

    s = _session(cfg)
    home_html = _fetch_html(s, website, timeout_s=cfg.timeout_s)
    if not home_html:
        return []

    soup = BeautifulSoup(home_html, "lxml")
    pages = [website] + _candidate_info_pages(website, soup)

    found: list[str] = []
    visited: set[str] = set()

    for url in pages[: max(1, cfg.max_pages)]:
        if url in visited:
            continue
        visited.add(url)
        if cfg.sleep_s > 0:
            time.sleep(cfg.sleep_s)
        html = home_html if url == website else _fetch_html(s, url, timeout_s=cfg.timeout_s)
        if not html:
            continue
        if not _looks_like_company_site(html, company):
            continue
        soup2 = BeautifulSoup(html, "lxml")
        found.extend(_extract_names_from_soup(soup2))
        if len(found) >= 6:
            break

    # De-dupe
    out: list[str] = []
    seen: set[str] = set()
    for n in found:
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out[:10]
