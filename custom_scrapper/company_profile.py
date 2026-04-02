from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

import requests
from bs4 import BeautifulSoup


@dataclass(frozen=True)
class CompanyProfileData:
    profile_url: str
    company_name: str | None
    website: str | None
    team_members: list[str]


def _slugify(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


def _unique_keep_order(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def candidate_company_slugs(company_name: str) -> list[str]:
    base = _slugify(company_name)
    if not base:
        return []

    tokens = [t for t in base.split("-") if t]
    if not tokens:
        return []

    suffixes = {
        "inc",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "ag",
        "sarl",
        "pte",
        "pvt",
        "plc",
    }

    candidates: list[str] = [base]

    # Strip common legal suffixes from the end (Heartstamp Inc -> heartstamp).
    trimmed = tokens[:]
    while trimmed and trimmed[-1] in suffixes:
        trimmed = trimmed[:-1]
        if trimmed:
            candidates.append("-".join(trimmed))

    # Often the canonical slug is just the first token.
    if tokens:
        candidates.append(tokens[0])

    # Some companies include "ai" in the display name but not in slug.
    if tokens and tokens[-1] == "ai" and len(tokens) > 1:
        candidates.append("-".join(tokens[:-1]))

    return _unique_keep_order(candidates)


def _looks_blocked(html: str) -> bool:
    lowered = html.lower()
    return (
        "captcha-delivery.com" in lowered
        or "geo.captcha-delivery.com" in lowered
        or "ct.captcha-delivery.com" in lowered
        or "access is temporarily restricted" in lowered
        or "we detected unusual activity" in lowered
        or "automated (bot) activity" in lowered
    )


def _is_valid_http_url(url: str | None) -> bool:
    if not url or not isinstance(url, str):
        return False
    u = url.strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    # Filter obviously broken values created from titles.
    if any(ch in u for ch in (" ", "(", ")", "|")):
        return False
    return True


def _extract_company_name(soup: BeautifulSoup) -> str | None:
    og = soup.select_one("meta[property='og:title']")
    if og and isinstance(og.get("content"), str):
        t = og.get("content", "").strip()
        if t:
            # Often like: "Heartstamp - Company Profile | Wellfound"
            t = re.sub(r"\s*[|\-—–].*$", "", t).strip()
            return t or None

    h1 = soup.select_one("h1")
    if h1:
        t = " ".join(h1.get_text(" ", strip=True).split())
        return t or None

    return None


def _extract_website(soup: BeautifulSoup) -> str | None:
    # Prefer an explicit "Website" link.
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not isinstance(href, str):
            continue
        label = (a.get_text(" ", strip=True) or "").lower()
        if "website" in label and _is_valid_http_url(href):
            return href.strip()

    # Otherwise pick the first external link that doesn't look like social.
    blocked_domains = (
        "wellfound.com",
        "angel.co",
        "linkedin.com",
        "twitter.com",
        "x.com",
        "facebook.com",
        "instagram.com",
        "youtube.com",
        "github.com",
    )
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not isinstance(href, str):
            continue
        if not _is_valid_http_url(href):
            continue
        if any(d in href.lower() for d in blocked_domains):
            continue
        return href.strip()

    return None


_NAME_RE = re.compile(r"^[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)+$")


def _extract_team_members(soup: BeautifulSoup) -> list[str]:
    # Try to locate the section that contains "Meet your team".
    anchor = soup.find(string=re.compile(r"meet\s+your\s+team", re.IGNORECASE))

    candidates: list[str] = []

    def collect_from_container(container) -> None:
        if not container:
            return
        # Names are often in headings or link text.
        for el in container.select("h1,h2,h3,h4,a,span,div"):
            t = (el.get_text(" ", strip=True) or "").strip()
            if not t:
                continue
            t = " ".join(t.split())
            if len(t) < 4 or len(t) > 60:
                continue
            if _NAME_RE.match(t):
                candidates.append(t)

    if anchor:
        # Walk up a few parents to capture the section.
        node = anchor.parent
        for _ in range(4):
            if not node:
                break
            collect_from_container(node if hasattr(node, "select") else None)
            node = node.parent

    # Fallback: if we didn't find the anchor, scan for common "Team" sections.
    if not candidates:
        team_anchor = soup.find(string=re.compile(r"\bteam\b", re.IGNORECASE))
        if team_anchor:
            node = team_anchor.parent
            for _ in range(3):
                if not node:
                    break
                collect_from_container(node if hasattr(node, "select") else None)
                node = node.parent

    # De-dupe and remove obvious non-person labels.
    exclude = {
        "wellfound",
        "remote",
        "hybrid",
        "apply",
        "jobs",
        "open roles",
        "team",
        "meet your team",
    }

    out: list[str] = []
    seen: set[str] = set()
    for n in candidates:
        k = n.strip()
        if not k:
            continue
        if k.lower() in exclude:
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(k)

    # Cap to keep CSV tidy.
    return out[:20]


def fetch_company_profile(
    company_name: str,
    *,
    session: requests.Session | None = None,
    timeout_s: int = 20,
) -> CompanyProfileData | None:
    if not company_name or not company_name.strip():
        return None

    slugs = candidate_company_slugs(company_name)
    if not slugs:
        return None

    sess = session or requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    for slug in slugs:
        url = f"https://wellfound.com/company/{slug}"
        try:
            resp = sess.get(url, headers=headers, timeout=timeout_s, allow_redirects=True)
        except Exception:
            continue

        if resp.status_code != 200:
            continue

        html = resp.text or ""
        if _looks_blocked(html):
            continue

        soup = BeautifulSoup(html, "lxml")
        website = _extract_website(soup)
        team = _extract_team_members(soup)
        name = _extract_company_name(soup)
        return CompanyProfileData(profile_url=url, company_name=name, website=website, team_members=team)

    return None


def should_replace_company_website(existing: str | None) -> bool:
    # Replace if missing or obviously broken.
    if not existing or not existing.strip():
        return True
    return not _is_valid_http_url(existing)
