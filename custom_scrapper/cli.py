from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import time
import re

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from tqdm import tqdm

from .draft_email import build_draft_email
from .email_infer import infer_emails
from .models import JobLead
from .output import write_csv
from .query import QueryConfig, build_wellfound_google_query
from .serp import SerpResult, google_serp_search, google_serp_search_detailed
from .skill_match import score_skill_match
from .scrape import scrape_wellfound_job
from .stores import push_airtable, push_notion
from .enrich import SerpCache, discover_company_website, discover_contact_or_apply_links


DEFAULT_SKILLS = [
    "nodejs",
    "django",
    "fastapi",
    "mongodb",
    "postgresql",
    "aws",
    "docker",
    "kubernetes",
    "genai",
    "llm",
]

DEFAULT_CANDIDATE_SUMMARY = (
    "I’m a full-stack dev + AI engineer (agentic + GenAI) with 5+ years of experience across "
    "Node.js, Django/FastAPI, MongoDB/PostgreSQL, and AWS (Docker/Kubernetes)."
)


def _parse_role_company_from_serp_title(title: str | None) -> tuple[str | None, str | None]:
    if not title:
        return None, None
    t = title.strip()
    # Drop trailing site label if present.
    t = re.sub(r"\s*[|\-—–]\s*wellfound(\.com)?\s*$", "", t, flags=re.IGNORECASE)

    # Common variants (Google snippets vary a lot).
    # 1) "Role at Company • Location"
    # 2) "Role - Company"
    # 3) "Company - Role"

    # Common Wellfound pattern: "Role at Company • Location"
    m = re.match(r"^(?P<role>.+?)\s+at\s+(?P<company>.+?)(?:\s+•\s+.+)?$", t, flags=re.IGNORECASE)
    if m:
        role = m.group("role").strip()
        company = m.group("company").strip()
        return role or None, company or None

    m = re.match(r"^(?P<role>.+?)\s*[|\-—–]\s*(?P<company>.+?)$", t)
    if m:
        role = m.group("role").strip()
        company = m.group("company").strip()
        if role and company:
            return role, company

    m = re.match(r"^(?P<company>.+?)\s*[|\-—–]\s*(?P<role>.+?)$", t)
    if m:
        role = m.group("role").strip()
        company = m.group("company").strip()
        if role and company:
            return role, company

    return t or None, None


def _company_from_job_url(url: str) -> str | None:
    # Example: /jobs/4019760-heartstamp-inc-tech-lead-...
    m = re.search(r"/jobs/\d+-([a-z0-9-]+)$", url)
    if not m:
        return None
    slug = m.group(1)
    tokens = [t for t in slug.split("-") if t]
    if not tokens:
        return None

    stop = {
        "engineer",
        "developer",
        "dev",
        "tech",
        "lead",
        "manager",
        "director",
        "head",
        "founding",
        "backend",
        "frontend",
        "full",
        "stack",
        "data",
        "ai",
        "ml",
        "generative",
        "llm",
        "sre",
        "site",
        "reliability",
    }
    company_tokens: list[str] = []
    for tok in tokens:
        if tok in stop:
            break
        company_tokens.append(tok)
        if len(company_tokens) >= 5:
            break

    if not company_tokens:
        return None
    return " ".join([t.capitalize() for t in company_tokens])


def _skills_from_text(text: str, target_skills: list[str]) -> list[str]:
    lower = text.lower()
    found: list[str] = []
    for s in target_skills:
        k = s.strip().lower()
        if not k:
            continue
        aliases = {k}
        if k in {"node", "nodejs", "node.js"}:
            aliases |= {"nodejs", "node.js", "node js"}
        if k in {"kubernetes", "k8s"}:
            aliases |= {"kubernetes", "k8s"}
        if k in {"postgresql", "postgres"}:
            aliases |= {"postgresql", "postgres"}
        if k in {"genai", "gen ai", "generative ai"}:
            aliases |= {"genai", "gen ai", "generative ai"}
        if k in {"llm", "llms"}:
            aliases |= {"llm", "llms", "large language model"}

        if any(a in lower for a in aliases):
            found.append(s)
    return found


def _extract_compensation_text(text: str | None) -> str | None:
    if not text:
        return None
    t = " ".join(text.split())
    # Best-effort: match common comp patterns seen in SERP snippets.
    # Examples: "$20k/mo", "$150k-$200k", "USD 180k/year".
    pat = re.compile(
        r"(?i)(?:\$|usd\s*)\s*\d[\d,]*(?:\s*[km])?(?:\s*[-–—]\s*(?:\$|usd\s*)?\s*\d[\d,]*(?:\s*[km])?)?(?:\s*/\s*(?:year|yr|annum|month|mo|week|wk|hour|hr))?"
    )
    m = pat.search(t)
    if not m:
        return None
    return m.group(0).strip()


def _lead_from_serp(sr: SerpResult, target_skills: list[str]) -> JobLead:
    lead = JobLead(url=sr.url)
    role, company = _parse_role_company_from_serp_title(sr.title)
    lead.title = role
    lead.company = company or _company_from_job_url(sr.url)
    lead.description = sr.snippet
    combined = f"{sr.title or ''} {sr.snippet or ''}".strip()
    lead.required_skills = _skills_from_text(combined, target_skills)
    lead.compensation_text = _extract_compensation_text(sr.snippet) or _extract_compensation_text(sr.title)
    lead.raw = {"serp_title": sr.title, "serp_snippet": sr.snippet}
    return lead


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="custom_scrapper", description="Wellfound lead scraper via SerpAPI + Playwright")

    p.add_argument("--skills", default=",".join(DEFAULT_SKILLS), help="Comma-separated target skills")
    p.add_argument("--posted", default="today", help="Date phrase for query: today|past_24h|... (query text)")
    p.add_argument("--min-comp-usd", type=int, default=20000, help="Minimum comp used in query text")
    p.add_argument("--comp-period", choices=["month", "year"], default="month", help="Comp period for query text")
    p.add_argument(
        "--max-stage",
        choices=["pre_revenue", "seed", "series_a", "series_b"],
        default="series_b",
        help="Max company stage (used in query text)",
    )

    p.add_argument("--max-urls", type=int, default=100, help="Max job URLs to fetch from SerpAPI")
    p.add_argument("--min-match", type=float, default=0.35, help="Drop jobs with match_score below this")
    p.add_argument(
        "--serp-only",
        action="store_true",
        help="Do not open Wellfound pages; write CSV from SerpAPI titles/snippets only (works even when Wellfound blocks).",
    )
    p.add_argument(
        "--fallback-serp",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When scraping is blocked, keep the row using SERP title/snippet instead of dropping it.",
    )

    p.add_argument(
        "--enrich",
        action="store_true",
        help="Enrich rows via extra SerpAPI queries: company website + apply/contact/Google Form links (uses more credits).",
    )
    p.add_argument(
        "--serp-sleep",
        type=float,
        default=0.0,
        help="Sleep seconds between SerpAPI requests (helps rate limits).",
    )

    p.add_argument(
        "--urls-file",
        default=None,
        help="Optional text file with one URL per line; when provided, SerpAPI is skipped.",
    )
    p.add_argument("--print-query", action="store_true", help="Print the generated Google query and exit")

    p.add_argument("--out", default="output/jobs.csv", help="Output CSV path")
    p.add_argument("--airtable", action="store_true", help="Also push rows to Airtable (requires env vars)")
    p.add_argument("--notion", action="store_true", help="Also push rows to Notion (requires env vars)")
    p.add_argument("--headful", action="store_true", help="Run browser in headed mode")
    p.add_argument(
        "--user-data-dir",
        default="playwright_profile",
        help="Playwright persistent profile dir (stores cookies; useful for CAPTCHA/login).",
    )
    p.add_argument(
        "--captcha-wait",
        type=int,
        default=180,
        help="Seconds to wait for manual CAPTCHA solve when blocked (headful recommended).",
    )

    p.add_argument(
        "--candidate-summary",
        default=DEFAULT_CANDIDATE_SUMMARY,
        help="One-liner used in the draft email body",
    )

    p.add_argument(
        "--debug-dir",
        default=None,
        help="When scraping fails to extract fields, dump HTML/screenshot here for debugging.",
    )

    return p.parse_args()


async def _run_async(args: argparse.Namespace) -> int:
    skills = [s.strip() for s in (args.skills or "").split(",") if s.strip()]

    cfg = QueryConfig(
        skills=skills,
        posted=args.posted,
        min_comp_usd=args.min_comp_usd,
        comp_period=args.comp_period,
        company_stage=args.max_stage,
    )

    query = build_wellfound_google_query(cfg)
    if args.print_query:
        print(query)
        return 0

    serp_results: list[SerpResult] = []

    if args.urls_file:
        url_text = Path(args.urls_file).read_text(encoding="utf-8")
        urls = [line.strip() for line in url_text.splitlines() if line.strip() and not line.strip().startswith("#")]
        serp_results = [SerpResult(url=u) for u in urls]
    else:
        serp_results = google_serp_search_detailed(query, max_results=args.max_urls, sleep_s=args.serp_sleep)
        urls = [r.url for r in serp_results]

    serp_by_url = {r.url: r for r in serp_results}

    leads: list[JobLead] = []

    if args.serp_only:
        cache = SerpCache()
        for sr in serp_results:
            lead = _lead_from_serp(sr, skills)
            score, matched = score_skill_match(skills, lead.required_skills)
            lead.match_score = score
            lead.matched_skills = matched
            if score < args.min_match:
                continue

            # Enrich only for rows that pass skill match (saves SerpAPI credits).
            if args.enrich and lead.company:
                lead.company_website = discover_company_website(lead.company, cache=cache, sleep_s=args.serp_sleep)
                links = discover_contact_or_apply_links(
                    company=lead.company,
                    role=lead.title,
                    cache=cache,
                    sleep_s=args.serp_sleep,
                )
                lead.apply_url = links.get("apply_url")
                lead.contact_url = links.get("contact_url")
                lead.google_form_url = links.get("google_form_url")

            lead.inferred_emails = infer_emails(lead.contact_name, lead.company_website)
            lead.draft_email = build_draft_email(lead, candidate_summary=args.candidate_summary)
            leads.append(lead)

        write_csv(leads, args.out)
        return 0

    async with async_playwright() as p:
        user_data_dir = args.user_data_dir
        lock_path = Path(user_data_dir) / "SingletonLock"
        if lock_path.exists():
            # Profile likely in use (or Chromium crashed). Avoid corruption by using a fresh dir.
            user_data_dir = f"{user_data_dir}_{int(time.time())}"

        # Persistent context keeps cookies/storage (helps after manual CAPTCHA solve).
        # A realistic UA/viewport also reduces false bot flags.
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=not args.headful,
            viewport={"width": 1365, "height": 768},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        )
        try:
            for url in tqdm(urls, desc="Scraping", unit="url"):
                try:
                    lead = await scrape_wellfound_job(
                        context,
                        url,
                        allow_manual_captcha=args.headful,
                        manual_captcha_timeout_s=args.captcha_wait,
                        debug_dir=args.debug_dir,
                    )
                except Exception as e:
                    msg = str(e)
                    lowered = msg.lower()
                    sr = serp_by_url.get(url)
                    if args.fallback_serp and sr and (
                        "captcha" in lowered
                        or "temporarily restricted" in lowered
                        or "bot protection" in lowered
                        or "unusual activity" in lowered
                    ):
                        lead = _lead_from_serp(sr, skills)
                        lead.raw["scrape_error"] = msg
                        score, matched = score_skill_match(skills, lead.required_skills)
                        lead.match_score = score
                        lead.matched_skills = matched
                        if score >= args.min_match:
                            lead.inferred_emails = infer_emails(lead.contact_name, lead.company_website)
                            lead.draft_email = build_draft_email(lead, candidate_summary=args.candidate_summary)
                            leads.append(lead)
                        continue

                    leads.append(JobLead(url=url, raw={"error": msg}))
                    continue

                score, matched = score_skill_match(skills, lead.required_skills)
                lead.match_score = score
                lead.matched_skills = matched

                if score < args.min_match:
                    continue

                lead.inferred_emails = infer_emails(lead.contact_name, lead.company_website)
                lead.draft_email = build_draft_email(lead, candidate_summary=args.candidate_summary)

                leads.append(lead)
        finally:
            await context.close()

    write_csv(leads, args.out)

    if args.airtable:
        push_airtable(leads)
    if args.notion:
        push_notion(leads)
    return 0


def main() -> int:
    load_dotenv(override=False)
    args = _parse_args()
    return asyncio.run(_run_async(args))
