from __future__ import annotations

import re
from urllib.parse import urlparse


def _normalize_domain(website_url: str) -> str | None:
    if not website_url:
        return None
    try:
        p = urlparse(website_url)
    except Exception:
        return None
    host = (p.netloc or "").lower()
    if not host:
        return None
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if "." not in host:
        return None
    return host


def _name_parts(full_name: str) -> tuple[str | None, str | None]:
    if not full_name:
        return None, None
    cleaned = re.sub(r"[^A-Za-z ]+", " ", full_name)
    parts = [p for p in cleaned.strip().split() if p]
    if len(parts) < 2:
        return None, None
    return parts[0].lower(), parts[-1].lower()


def infer_common_emails(contact_name: str | None, company_website: str | None) -> list[str]:
    domain = _normalize_domain(company_website or "")
    first, last = _name_parts(contact_name or "")
    if not domain or not first or not last:
        return []

    fi = first[0]
    li = last[0]

    patterns = [
        f"{first}@{domain}",
        f"{first}.{last}@{domain}",
        f"{first}_{last}@{domain}",
        f"{first}{last}@{domain}",
        f"{fi}{last}@{domain}",
        f"{first}{li}@{domain}",
        f"{last}.{first}@{domain}",
        f"{last}@{domain}",
    ]

    # De-dupe preserving order
    out: list[str] = []
    seen: set[str] = set()
    for e in patterns:
        if e not in seen:
            seen.add(e)
            out.append(e)

    return out


def infer_generic_emails(company_website: str | None) -> list[str]:
    domain = _normalize_domain(company_website or "")
    if not domain:
        return []
    locals_ = [
        "careers",
        "jobs",
        "recruiting",
        "talent",
        "talentacquisition",
        "people",
        "hr",
        "hello",
        "info",
    ]
    return [f"{l}@{domain}" for l in locals_]


def infer_emails(contact_name: str | None, company_website: str | None) -> list[str]:
    # Prefer name-based patterns; otherwise provide generic role-based addresses.
    emails = infer_common_emails(contact_name, company_website)
    if emails:
        return emails
    return infer_generic_emails(company_website)
