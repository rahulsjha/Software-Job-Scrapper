from __future__ import annotations

import argparse
import asyncio
import csv
import json
from pathlib import Path
import time
import re
import random

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
from .scrape import scrape_wellfound_job, fetch_company_profile_from_wellfound
from .stores import push_airtable, push_notion
from .enrich import SerpCache, discover_company_website, discover_contact_or_apply_links
from .company_profile import fetch_company_profile, should_replace_company_website
from .web_enrich import WebEnrichConfig, guess_company_website, discover_team_members


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


def _playwright_browser_type(p, name: str):
    n = (name or "").strip().lower()
    if n == "firefox":
        return p.firefox
    if n == "webkit":
        return p.webkit
    return p.chromium


def _default_user_agent_for_browser(name: str) -> str:
    n = (name or "").strip().lower()
    if n == "firefox":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) "
            "Gecko/20100101 Firefox/123.0"
        )
    if n == "webkit":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4 Safari/605.1.15"
        )
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
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
        "--web-enrich",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Offline enrichment that does not use SerpAPI/Wellfound: guesses company website by probing common domains, "
            "then extracts team/contact names from the company site (/about,/team)."
        ),
    )
    p.add_argument("--web-timeout", type=float, default=8.0, help="Timeout seconds per website request in --web-enrich")
    p.add_argument("--web-pages", type=int, default=4, help="Max pages to fetch per company in --web-enrich")
    p.add_argument("--web-sleep", type=float, default=0.5, help="Sleep seconds between website fetches in --web-enrich")

    p.add_argument(
        "--company-profile-enrich",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Enrich via https://wellfound.com/company/<slug>: fills company_profile_url, company_website, and team_members when available."
        ),
    )

    p.add_argument(
        "--company-profile-playwright",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Use Playwright to fetch Wellfound /company/<slug> pages for enrichment. "
            "Recommended when Wellfound blocks plain HTTP with 403/CAPTCHA."
        ),
    )

    p.add_argument(
        "--enrich-csv",
        default=None,
        help=(
            "Path to an existing jobs CSV to enrich (no SerpAPI). "
            "Reads rows, optionally fetches Wellfound company profile pages, and writes to --out."
        ),
    )
    p.add_argument(
        "--serp-sleep",
        type=float,
        default=0.0,
        help="Sleep seconds between SerpAPI requests (helps rate limits).",
    )

    p.add_argument(
        "--wellfound-sleep",
        type=float,
        default=1.5,
        help="Base sleep seconds between Wellfound page navigations (helps avoid bot flags).",
    )
    p.add_argument(
        "--wellfound-jitter",
        type=float,
        default=1.0,
        help="Random +/- jitter added to --wellfound-sleep.",
    )
    p.add_argument(
        "--stop-on-restricted",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stop early if Wellfound shows 'Access is temporarily restricted'.",
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
        "--browser",
        choices=["chromium", "firefox", "webkit"],
        default="firefox",
        help="Playwright browser engine to use for scraping/enrichment.",
    )

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


async def _sleep_wellfound(args: argparse.Namespace) -> None:
    base = float(getattr(args, "wellfound_sleep", 0.0) or 0.0)
    jitter = float(getattr(args, "wellfound_jitter", 0.0) or 0.0)
    if base <= 0 and jitter <= 0:
        return
    low = max(0.0, base - jitter)
    high = base + jitter
    await asyncio.sleep(random.uniform(low, high))


def _split_csv_list(s: str | None) -> list[str]:
    if not s or not isinstance(s, str):
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def _lead_from_csv_row(row: dict[str, str]) -> JobLead:
    lead = JobLead(url=(row.get("url") or "").strip())
    lead.title = (row.get("title") or "").strip() or None
    lead.company = (row.get("company") or "").strip() or None
    lead.description = (row.get("description") or "").strip() or None

    lead.company_profile_url = (row.get("company_profile_url") or "").strip() or None
    lead.company_website = (row.get("company_website") or "").strip() or None
    lead.team_members = _split_csv_list(row.get("team_members"))

    lead.contact_name = (row.get("contact_name") or "").strip() or None
    lead.apply_url = (row.get("apply_url") or "").strip() or None
    lead.contact_url = (row.get("contact_url") or "").strip() or None
    lead.google_form_url = (row.get("google_form_url") or "").strip() or None
    lead.compensation_text = (row.get("compensation_text") or "").strip() or None
    lead.required_skills = _split_csv_list(row.get("required_skills"))

    ms = (row.get("match_score") or "").strip()
    try:
        lead.match_score = float(ms) if ms else None
    except Exception:
        lead.match_score = None
    lead.matched_skills = _split_csv_list(row.get("matched_skills"))
    lead.inferred_emails = _split_csv_list(row.get("inferred_emails"))
    lead.draft_email = (row.get("draft_email") or "").strip() or None
    lead.error = (row.get("error") or "").strip() or None

    raw = (row.get("raw") or "").strip()
    if raw:
        try:
            lead.raw = json.loads(raw)
        except Exception:
            lead.raw = {"raw": raw}
    return lead


def _enrich_lead_from_company_profile(lead: JobLead, *, cache: dict[str, dict] | None = None) -> None:
    if not lead.company:
        return
    cache = cache if cache is not None else {}
    key = lead.company.strip().lower()
    if key in cache:
        data = cache[key]
    else:
        cp = fetch_company_profile(lead.company)
        data = {
            "profile_url": cp.profile_url if cp else None,
            "company_name": cp.company_name if cp else None,
            "website": cp.website if cp else None,
            "team_members": cp.team_members if cp else [],
        }
        cache[key] = data

    if data.get("profile_url"):
        lead.company_profile_url = lead.company_profile_url or data["profile_url"]

    if should_replace_company_website(lead.company_website) and data.get("website"):
        lead.company_website = data["website"]

    team_members = data.get("team_members") or []
    if team_members and not lead.team_members:
        lead.team_members = team_members

    if not lead.contact_name and lead.team_members:
        # Best-effort: first person from "Meet your team".
        lead.contact_name = lead.team_members[0]

    if data.get("company_name") and (not lead.company or len((lead.company or "").strip()) < 2):
        lead.company = data["company_name"]

    if lead.raw is None:
        lead.raw = {}
    if isinstance(lead.raw, dict):
        lead.raw.setdefault("company_profile_url", lead.company_profile_url)
        if lead.team_members:
            lead.raw.setdefault("team_members", lead.team_members)


async def _run_async(args: argparse.Namespace) -> int:
    skills = [s.strip() for s in (args.skills or "").split(",") if s.strip()]
    web_cfg = WebEnrichConfig(timeout_s=float(args.web_timeout), max_pages=int(args.web_pages), sleep_s=float(args.web_sleep))

    # CSV-only enrichment mode: no SerpAPI and no Playwright.
    if args.enrich_csv:
        in_path = Path(args.enrich_csv)
        if not in_path.exists():
            raise SystemExit(f"CSV not found: {in_path}")

        with in_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            leads = [_lead_from_csv_row(r) for r in reader]

        if args.company_profile_enrich:
            # Prefer Playwright when requested, since Wellfound blocks direct HTTP.
            if args.company_profile_playwright:
                async with async_playwright() as p:
                    browser_type = _playwright_browser_type(p, args.browser)
                    user_data_dir = args.user_data_dir
                    lock_path = Path(user_data_dir) / "SingletonLock"
                    if lock_path.exists():
                        user_data_dir = f"{user_data_dir}_{int(time.time())}"

                    context = await browser_type.launch_persistent_context(
                        user_data_dir,
                        headless=not args.headful,
                        viewport={"width": 1365, "height": 768},
                        locale="en-US",
                        user_agent=_default_user_agent_for_browser(args.browser),
                    )
                    try:
                        for lead in tqdm(leads, desc="Company profiles", unit="row"):
                            if not lead.company:
                                continue
                            await _sleep_wellfound(args)
                            try:
                                data = await fetch_company_profile_from_wellfound(
                                    context,
                                    lead.company,
                                    allow_manual_captcha=args.headful,
                                    manual_captcha_timeout_s=args.captcha_wait,
                                )
                            except Exception as e:
                                if args.stop_on_restricted and "temporarily restricted" in str(e).lower():
                                    if lead.raw is None:
                                        lead.raw = {}
                                    if isinstance(lead.raw, dict):
                                        lead.raw["company_profile_restricted"] = True
                                    break
                                raise
                            if not data:
                                continue
                            lead.company_profile_url = lead.company_profile_url or (data.get("company_profile_url") or None)  # type: ignore[assignment]
                            if should_replace_company_website(lead.company_website):
                                lead.company_website = (data.get("company_website") or None)  # type: ignore[assignment]
                            team = data.get("team_members") or []
                            if team and not lead.team_members:
                                lead.team_members = team  # type: ignore[assignment]
                            if not lead.contact_name and lead.team_members:
                                lead.contact_name = lead.team_members[0]
                            if data.get("company") and not lead.company:
                                lead.company = data.get("company")  # type: ignore[assignment]
                    finally:
                        await context.close()
            else:
                cp_cache: dict[str, dict] = {}
                for lead in tqdm(leads, desc="Company profiles", unit="row"):
                    _enrich_lead_from_company_profile(lead, cache=cp_cache)

        if args.web_enrich:
            for lead in tqdm(leads, desc="Website/team (offline)", unit="row"):
                if not lead.company:
                    continue
                if should_replace_company_website(lead.company_website):
                    lead.company_website = guess_company_website(lead.company, cfg=web_cfg)
                if not lead.contact_name and lead.company_website:
                    team = discover_team_members(lead.company_website, lead.company, cfg=web_cfg)
                    if team:
                        if not lead.team_members:
                            lead.team_members = team
                        lead.contact_name = team[0]

        write_csv(leads, args.out)
        return 0

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
        serp_results = google_serp_search_detailed(
            query,
            max_results=args.max_urls,
            page_size=min(10, args.max_urls),
            sleep_s=args.serp_sleep,
            single_request=False,
        )
        urls = [r.url for r in serp_results]

    serp_by_url = {r.url: r for r in serp_results}

    leads: list[JobLead] = []

    if args.serp_only:
        cache = SerpCache()
        cp_cache: dict[str, dict] = {}
        pending_profile: list[JobLead] = []

        for sr in serp_results:
            lead = _lead_from_serp(sr, skills)
            score, matched = score_skill_match(skills, lead.required_skills)
            lead.match_score = score
            lead.matched_skills = matched
            if score < args.min_match:
                continue

            if False and args.company_profile_enrich and (not args.company_profile_playwright):
                # Plain HTTP fallback (often blocked by Wellfound).
                _enrich_lead_from_company_profile(lead, cache=cp_cache)

            # We'll enrich via Playwright in a batch after collecting leads.
            if False and args.company_profile_enrich and args.company_profile_playwright and lead.company:
                pending_profile.append(lead)

            # Enrich only for rows that pass skill match (saves SerpAPI credits).
            if False and args.enrich and lead.company:
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

            # Offline enrichment (works without SerpAPI credits and without Wellfound access).
            if False and args.web_enrich and lead.company:
                if should_replace_company_website(lead.company_website):
                    lead.company_website = guess_company_website(lead.company, cfg=web_cfg)
                if not lead.contact_name and lead.company_website:
                    team = discover_team_members(lead.company_website, lead.company, cfg=web_cfg)
                    if team:
                        lead.team_members = lead.team_members or team
                        lead.contact_name = team[0]

            lead.inferred_emails = infer_emails(lead.contact_name, lead.company_website)
            lead.draft_email = build_draft_email(lead, candidate_summary=args.candidate_summary)
            leads.append(lead)

        if args.company_profile_enrich and args.company_profile_playwright and pending_profile:
            async with async_playwright() as p:
                browser_type = _playwright_browser_type(p, args.browser)
                user_data_dir = args.user_data_dir
                lock_path = Path(user_data_dir) / "SingletonLock"
                if lock_path.exists():
                    user_data_dir = f"{user_data_dir}_{int(time.time())}"

                context = await browser_type.launch_persistent_context(
                    user_data_dir,
                    headless=not args.headful,
                    viewport={"width": 1365, "height": 768},
                    locale="en-US",
                    user_agent=_default_user_agent_for_browser(args.browser),
                )
                try:
                    for lead in tqdm(pending_profile, desc="Company profiles", unit="row"):
                        await _sleep_wellfound(args)
                        try:
                            data = await fetch_company_profile_from_wellfound(
                                context,
                                lead.company,
                                allow_manual_captcha=args.headful,
                                manual_captcha_timeout_s=args.captcha_wait,
                            )
                        except Exception as e:
                            if args.stop_on_restricted and "temporarily restricted" in str(e).lower():
                                if lead.raw is None:
                                    lead.raw = {}
                                if isinstance(lead.raw, dict):
                                    lead.raw["company_profile_restricted"] = True
                                break
                            raise
                        if not data:
                            continue
                        lead.company_profile_url = lead.company_profile_url or (data.get("company_profile_url") or None)  # type: ignore[assignment]
                        if data.get("company_website") and should_replace_company_website(lead.company_website):
                            lead.company_website = (data.get("company_website") or None)  # type: ignore[assignment]
                        team = data.get("team_members") or []
                        if team and not lead.team_members:
                            lead.team_members = team  # type: ignore[assignment]
                        if not lead.contact_name and lead.team_members:
                            lead.contact_name = lead.team_members[0]
                finally:
                    await context.close()

        write_csv(leads, args.out)
        return 0

    async with async_playwright() as p:
        browser_type = _playwright_browser_type(p, args.browser)
        user_data_dir = args.user_data_dir
        lock_path = Path(user_data_dir) / "SingletonLock"
        if lock_path.exists():
            # Profile likely in use (or Chromium crashed). Avoid corruption by using a fresh dir.
            user_data_dir = f"{user_data_dir}_{int(time.time())}"

        # Persistent context keeps cookies/storage (helps after manual CAPTCHA solve).
        # A realistic UA/viewport also reduces false bot flags.
        context = await browser_type.launch_persistent_context(
            user_data_dir,
            headless=not args.headful,
            viewport={"width": 1365, "height": 768},
            locale="en-US",
            user_agent=_default_user_agent_for_browser(args.browser),
        )
        try:
            for url in tqdm(urls, desc="Scraping", unit="url"):
                await _sleep_wellfound(args)
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

                    if args.stop_on_restricted and "temporarily restricted" in lowered:
                        leads.append(JobLead(url=url, raw={"error": msg, "restricted": True}))
                        break

                    sr = serp_by_url.get(url)
                    if args.fallback_serp and sr and (
                        "captcha" in lowered
                        or "temporarily restricted" in lowered
                        or "bot protection" in lowered
                        or "unusual activity" in lowered
                    ):
                        lead = _lead_from_serp(sr, skills)
                        lead.raw["scrape_error"] = msg
                        if "temporarily restricted" in lowered:
                            lead.raw["restricted"] = True
                        score, matched = score_skill_match(skills, lead.required_skills)
                        lead.match_score = score
                        lead.matched_skills = matched
                        if score >= args.min_match:
                            if args.company_profile_enrich:
                                # Use the already-open Playwright context to fetch /company/<slug>.
                                await _sleep_wellfound(args)
                                try:
                                    data = await fetch_company_profile_from_wellfound(
                                        context,
                                        lead.company,
                                        allow_manual_captcha=args.headful,
                                        manual_captcha_timeout_s=args.captcha_wait,
                                    )
                                except Exception as e2:
                                    if "temporarily restricted" in str(e2).lower():
                                        lead.raw["company_profile_restricted"] = True
                                        if args.stop_on_restricted:
                                            leads.append(lead)
                                            break
                                    data = None

                                if data:
                                    lead.company_profile_url = lead.company_profile_url or (data.get("company_profile_url") or None)  # type: ignore[assignment]
                                    if should_replace_company_website(lead.company_website):
                                        lead.company_website = (data.get("company_website") or None)  # type: ignore[assignment]
                                    team = data.get("team_members") or []
                                    if team and not lead.team_members:
                                        lead.team_members = team  # type: ignore[assignment]
                                    if not lead.contact_name and lead.team_members:
                                        lead.contact_name = lead.team_members[0]
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

                if args.company_profile_enrich and lead.company:
                    await _sleep_wellfound(args)
                    try:
                        data = await fetch_company_profile_from_wellfound(
                            context,
                            lead.company,
                            allow_manual_captcha=args.headful,
                            manual_captcha_timeout_s=args.captcha_wait,
                        )
                    except Exception as e:
                        if "temporarily restricted" in str(e).lower():
                            if lead.raw is None:
                                lead.raw = {}
                            if isinstance(lead.raw, dict):
                                lead.raw["company_profile_restricted"] = True
                            if args.stop_on_restricted:
                                leads.append(lead)
                                break
                        data = None
                    if data:
                        lead.company_profile_url = lead.company_profile_url or (data.get("company_profile_url") or None)  # type: ignore[assignment]
                        if should_replace_company_website(lead.company_website):
                            lead.company_website = (data.get("company_website") or None)  # type: ignore[assignment]
                        team = data.get("team_members") or []
                        if team and not lead.team_members:
                            lead.team_members = team  # type: ignore[assignment]
                        if not lead.contact_name and lead.team_members:
                            lead.contact_name = lead.team_members[0]

                # Offline enrichment fallback if Wellfound is blocked.
                if args.web_enrich and lead.company:
                    if should_replace_company_website(lead.company_website):
                        lead.company_website = guess_company_website(lead.company, cfg=web_cfg)
                    if not lead.contact_name and lead.company_website:
                        team = discover_team_members(lead.company_website, lead.company, cfg=web_cfg)
                        if team:
                            if not lead.team_members:
                                lead.team_members = team
                            lead.contact_name = team[0]

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
