from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class JobLead:
    url: str
    title: str | None = None
    company: str | None = None
    description: str | None = None

    company_profile_url: str | None = None
    company_website: str | None = None
    team_members: list[str] = field(default_factory=list)

    contact_name: str | None = None
    apply_url: str | None = None
    contact_url: str | None = None
    google_form_url: str | None = None
    compensation_text: str | None = None
    required_skills: list[str] = field(default_factory=list)

    match_score: float | None = None
    matched_skills: list[str] = field(default_factory=list)

    inferred_emails: list[str] = field(default_factory=list)
    draft_email: str | None = None

    error: str | None = None

    raw: dict[str, Any] = field(default_factory=dict)
