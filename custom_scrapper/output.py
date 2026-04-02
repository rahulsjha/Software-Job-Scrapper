from __future__ import annotations

import csv
import os
from pathlib import Path

from .models import JobLead
from .scrape import lead_to_dict


def write_csv(leads: list[JobLead], out_path: str) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    rows = [lead_to_dict(l) for l in leads]
    if not rows:
        # Still create the file with headers
        headers = [
            "url",
            "title",
            "company",
            "description",
            "company_profile_url",
            "company_website",
            "team_members",
            "contact_name",
            "apply_url",
            "contact_url",
            "google_form_url",
            "compensation_text",
            "required_skills",
            "match_score",
            "matched_skills",
            "inferred_emails",
            "draft_email",
            "error",
            "raw",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
        return

    # stable ordering
    fieldnames = list(rows[0].keys())

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    os.chmod(path, 0o644)
