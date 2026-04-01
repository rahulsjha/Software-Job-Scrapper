from __future__ import annotations

from .models import JobLead


def build_draft_email(lead: JobLead, *, candidate_summary: str) -> str:
    company = lead.company or "your team"
    title = lead.title or "the role"
    contact = lead.contact_name or "there"

    return (
        f"Hi {contact},\n\n"
        f"I saw {title} at {company} on Wellfound and it looks like a strong fit. "
        f"{candidate_summary}\n\n"
        "If you’re open to it, I’d love to share 2–3 relevant projects and see if it makes sense to chat this week.\n\n"
        "Best,\n"
        "<YOUR_NAME>"
    )
