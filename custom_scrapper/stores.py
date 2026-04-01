from __future__ import annotations

import os
from typing import Any

import requests

from .models import JobLead


class StoreConfigError(RuntimeError):
    pass


def _lead_fields(lead: JobLead) -> dict[str, Any]:
    return {
        "url": lead.url,
        "title": lead.title,
        "company": lead.company,
        "company_website": lead.company_website,
        "contact_name": lead.contact_name,
        "compensation_text": lead.compensation_text,
        "required_skills": ", ".join(lead.required_skills),
        "match_score": lead.match_score,
        "matched_skills": ", ".join(lead.matched_skills),
        "inferred_emails": ", ".join(lead.inferred_emails),
        "draft_email": lead.draft_email,
    }


def push_airtable(leads: list[JobLead]) -> None:
    api_key = os.getenv("AIRTABLE_API_KEY")
    base_id = os.getenv("AIRTABLE_BASE_ID")
    table = os.getenv("AIRTABLE_TABLE") or os.getenv("AIRTABLE_TABLE_NAME")

    if not (api_key and base_id and table):
        raise StoreConfigError(
            "Airtable requires AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE (or AIRTABLE_TABLE_NAME)"
        )

    url = f"https://api.airtable.com/v0/{base_id}/{table}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Airtable: up to 10 records per request
    batch: list[dict[str, Any]] = []
    for lead in leads:
        # skip error-only leads
        if lead.title is None and lead.company is None and lead.description is None and lead.match_score is None:
            continue

        batch.append({"fields": _lead_fields(lead)})
        if len(batch) == 10:
            _airtable_post(url, headers, batch)
            batch = []

    if batch:
        _airtable_post(url, headers, batch)


def _airtable_post(url: str, headers: dict[str, str], records: list[dict[str, Any]]) -> None:
    resp = requests.post(url, headers=headers, json={"records": records, "typecast": True}, timeout=60)
    if resp.status_code >= 300:
        raise RuntimeError(f"Airtable error {resp.status_code}: {resp.text[:200]}")


def push_notion(leads: list[JobLead]) -> None:
    api_key = os.getenv("NOTION_API_KEY")
    database_id = os.getenv("NOTION_DATABASE_ID")
    title_prop = os.getenv("NOTION_TITLE_PROP", "Name")

    if not (api_key and database_id):
        raise StoreConfigError("Notion requires NOTION_API_KEY and NOTION_DATABASE_ID")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    for lead in leads:
        if lead.title is None and lead.company is None and lead.description is None and lead.match_score is None:
            continue

        name = f"{lead.company or 'Company'} — {lead.title or 'Role'}"
        properties: dict[str, Any] = {
            title_prop: {"title": [{"text": {"content": name}}]},
            "URL": {"url": lead.url},
            "Company": {"rich_text": [{"text": {"content": lead.company or ""}}]},
            "Contact": {"rich_text": [{"text": {"content": lead.contact_name or ""}}]},
            "Emails": {"rich_text": [{"text": {"content": ", ".join(lead.inferred_emails)}}]},
            "Match Score": {"number": float(lead.match_score or 0.0)},
        }

        payload = {
            "parent": {"database_id": database_id},
            "properties": properties,
        }

        resp = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=60)
        if resp.status_code >= 300:
            # Most common failure is missing properties in the DB schema.
            raise RuntimeError(
                f"Notion error {resp.status_code}: {resp.text[:200]} (ensure database has properties: {list(properties.keys())})"
            )
