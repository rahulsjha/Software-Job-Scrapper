from __future__ import annotations

import asyncio
import re
from dataclasses import asdict
from pathlib import Path
from typing import Iterable
import json

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, Error as PlaywrightError, Page

from .models import JobLead
from .company_profile import candidate_company_slugs


def _is_captcha_page(html: str) -> bool:
    lowered = html.lower()
    return (
        "captcha-delivery.com" in lowered
        or "geo.captcha-delivery.com" in lowered
        or "ct.captcha-delivery.com" in lowered
        or "data-cfasync" in lowered and "captcha" in lowered
    )


def _is_access_restricted_page(html: str) -> bool:
    lowered = html.lower()
    return (
        "access is temporarily restricted" in lowered
        or "we detected unusual activity" in lowered
        or "automated (bot) activity" in lowered
        or "unusual activity from your device" in lowered
    )


def _extract_meta_content(soup: BeautifulSoup, *, property_name: str) -> str | None:
    el = soup.select_one(f"meta[property='{property_name}']")
    if not el:
        return None
    content = el.get("content")
    if not content or not isinstance(content, str):
        return None
    return _clean_text(content)


async def _safe_page_content(page: Page, *, attempts: int = 10, delay_s: float = 0.5) -> str:
    last_error: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            return await page.content()
        except PlaywrightError as e:
            last_error = e
            # Happens frequently during challenge/captcha redirects.
            if "navigating" in str(e).lower() or "changing the content" in str(e).lower():
                await asyncio.sleep(delay_s)
                continue
            raise
    if last_error:
        raise last_error
    return await page.content()


async def auto_scroll(page: Page, *, max_rounds: int = 20, idle_ms: int = 800) -> None:
    last_height = await page.evaluate("() => document.body.scrollHeight")
    for _ in range(max_rounds):
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(idle_ms)
        new_height = await page.evaluate("() => document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height


def _company_name_to_slug(name: str) -> str:
    """Convert company name to Wellfound URL slug.
    Example: "C3 AI" -> "c3-ai", "Heartstamp Inc" -> "heartstamp-inc"
    """
    name = name.strip().lower()
    # Replace spaces and special chars with hyphens
    name = re.sub(r"[^a-z0-9]+", "-", name)
    # Remove trailing/leading hyphens
    name = name.strip("-")
    return name


async def _fetch_company_website_from_wellfound(
    context: BrowserContext, company_name: str | None, timeout_ms: int = 30000
) -> str | None:
    """Fetch company website from Wellfound company profile page.
    
    Visit https://wellfound.com/company/{slug} and extract the website link.
    """
    if not company_name:
        return None

    slug = _company_name_to_slug(company_name)
    company_url = f"https://wellfound.com/company/{slug}"

    page = await context.new_page()
    try:
        try:
            await page.goto(company_url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(800)
        except Exception:
            # Company profile might not exist.
            return None

        html = await _safe_page_content(page)
        soup = BeautifulSoup(html, "lxml")

        # Look for website link on company profile.
        for a in soup.select("a[href]"):
            href = a.get("href")
            if not href or not isinstance(href, str):
                continue
            label = (a.get_text(" ", strip=True) or "").lower()
            if "website" in label and href.startswith("http"):
                return href

        # Fallback: first external HTTP link (not Wellfound).
        for a in soup.select("a[href]"):
            href = a.get("href")
            if not href or not isinstance(href, str):
                continue
            if href.startswith("http") and "wellfound.com" not in href:
                return href

        return None
    except Exception:
        return None
    finally:
        try:
            await page.close()
        except Exception:
            pass


def _clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _extract_first_text(soup: BeautifulSoup, selectors: Iterable[str]) -> str | None:
    for sel in selectors:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            return _clean_text(el.get_text(" ", strip=True))
    return None


def _extract_compensation(text: str) -> str | None:
    # Best-effort extraction from full page text.
    patterns = [
        r"\$\s?\d{2,3}\s?k\s?(?:-\s?\$?\s?\d{2,3}\s?k)?\s?(?:/|per)\s?(?:year|yr|month|mo)",
        r"\$\s?\d{2,3}\s?k\s?(?:-\s?\$?\s?\d{2,3}\s?k)?",
        r"\$\s?\d{2,3}(?:,\d{3})?\s?(?:-\s?\$?\s?\d{2,3}(?:,\d{3})?)\s?(?:/|per)\s?(?:year|yr|month|mo)",
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            return _clean_text(m.group(0))
    return None


def _extract_company_website(soup: BeautifulSoup) -> str | None:
    # Look for obvious "Website" links.
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href or not isinstance(href, str):
            continue
        label = (a.get_text(" ", strip=True) or "").lower()
        if "website" in label or "visit website" in label:
            if href.startswith("http"):
                return href

    # Fallback: first external link that is not wellfound.
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href or not isinstance(href, str):
            continue
        if href.startswith("http") and "wellfound.com" not in href:
            return href

    return None


def _extract_apply_url(soup: BeautifulSoup) -> str | None:
    # Look for "Apply" button/link in the job posting.
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href or not isinstance(href, str):
            continue
        label = (a.get_text(" ", strip=True) or "").lower()
        if "apply" in label and href.startswith("http"):
            return href
    return None


def _extract_contact_url(soup: BeautifulSoup) -> str | None:
    # Look for "Contact" link on the company info section.
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href or not isinstance(href, str):
            continue
        label = (a.get_text(" ", strip=True) or "").lower()
        if ("contact" in label or "reach us" in label) and href.startswith("http"):
            return href
    return None


def _extract_google_form_url(soup: BeautifulSoup) -> str | None:
    # Look for Google Form link (forms.gle or docs.google.com/forms).
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href or not isinstance(href, str):
            continue
        if "forms.gle" in href or "docs.google.com/forms" in href:
            return href
    return None


def _extract_contact_name(text: str) -> str | None:
    # Best-effort patterns.
    # Examples: "Hiring Manager: Jane Doe" / "Recruiter John Smith" / "Contact: ..."
    patterns = [
        r"Hiring Manager\s*:?\s*([A-Z][a-z]+\s+[A-Z][a-z]+)",
        r"Recruiter\s*:?\s*([A-Z][a-z]+\s+[A-Z][a-z]+)",
        r"Contact\s*:?\s*([A-Z][a-z]+\s+[A-Z][a-z]+)",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return _clean_text(m.group(1))
    return None


def _extract_team_members_from_job_posting(soup: BeautifulSoup) -> list[str]:
    """Extract team member names from the job posting page itself (e.g., 'Your team', 'Meet the team', hiring manager names)."""
    names: list[str] = []
    name_re = re.compile(r"^[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)+$")
    
    # Look for common section headers
    for pattern in [r"your\s+team", r"meet\s+the\s+team", r"team\s+members", r"hiring\s+manager", r"posted\s+by"]:
        anchor = soup.find(string=re.compile(pattern, re.IGNORECASE))
        if anchor and getattr(anchor, "parent", None):
            node = anchor.parent
            for _ in range(5):  # check a few parent levels
                if not node or not hasattr(node, "select"):
                    break
                for el in node.select("h1,h2,h3,h4,h5,strong,a,span,div"):
                    t = (el.get_text(" ", strip=True) or "").strip()
                    t = " ".join(t.split())
                    if len(t) >= 4 and len(t) <= 60 and name_re.match(t):
                        names.append(t)
                node = getattr(node, "parent", None)
        if names:
            break
    
    # De-dupe and exclude false positives
    out: list[str] = []
    seen: set[str] = set()
    exclude = {"your team", "meet the team", "team members", "hiring manager", "posted by", "wellfound", "apply now"}
    for n in names:
        ln = n.lower()
        if ln in exclude or len(n.split()) > 3:
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out[:10]


def _extract_team_members_from_company_profile(soup: BeautifulSoup) -> list[str]:
    # Best-effort: look for "Meet your team" section.
    anchor = soup.find(string=re.compile(r"meet\s+your\s+team", re.IGNORECASE))
    names: list[str] = []
    name_re = re.compile(r"^[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)+$")

    def collect(container) -> None:
        if not container:
            return
        for el in container.select("h1,h2,h3,h4,a,span,div"):
            t = (el.get_text(" ", strip=True) or "").strip()
            t = " ".join(t.split())
            if len(t) < 4 or len(t) > 60:
                continue
            if name_re.match(t):
                names.append(t)

    if anchor and getattr(anchor, "parent", None):
        node = anchor.parent
        for _ in range(4):
            if not node or not hasattr(node, "select"):
                break
            collect(node)
            node = getattr(node, "parent", None)

    # De-dupe and cap.
    out: list[str] = []
    seen: set[str] = set()
    exclude = {"wellfound", "team", "meet your team", "apply", "jobs", "open roles"}
    for n in names:
        if n.lower() in exclude:
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out[:20]


async def fetch_company_profile_from_wellfound(
    context: BrowserContext,
    company_name: str | None,
    *,
    timeout_ms: int = 30000,
    allow_manual_captcha: bool = False,
    manual_captcha_timeout_s: int = 180,
) -> dict[str, object] | None:
    """Fetch company profile data from https://wellfound.com/company/<slug> using Playwright.

    This works even when direct HTTP requests are blocked, as long as the user can
    solve CAPTCHA once in headful mode and the persistent profile keeps cookies.
    """

    if not company_name:
        return None

    slugs = candidate_company_slugs(company_name)
    if not slugs:
        return None

    page = await context.new_page()
    try:
        for slug in slugs:
            url = f"https://wellfound.com/company/{slug}"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_timeout(800)
            except Exception:
                continue

            html = await _safe_page_content(page)

            if _is_access_restricted_page(html):
                raise RuntimeError(
                    "Wellfound returned 'Access is temporarily restricted'. "
                    "Company profiles cannot be scraped right now."
                )

            if _is_captcha_page(html):
                if not allow_manual_captcha:
                    continue

                print(
                    f"[CAPTCHA] Blocked by bot protection for {url}. "
                    f"Please complete the CAPTCHA in the opened browser window within {manual_captcha_timeout_s}s..."
                )
                try:
                    await page.bring_to_front()
                except Exception:
                    pass

                for _ in range(max(1, manual_captcha_timeout_s // 2)):
                    await asyncio.sleep(2)
                    if page.is_closed():
                        return None
                    html = await _safe_page_content(page)
                    if _is_access_restricted_page(html):
                        # Not solvable via CAPTCHA; treat as hard block.
                        raise RuntimeError(
                            "Wellfound returned 'Access is temporarily restricted' after the challenge. "
                            "Company profiles cannot be scraped right now."
                        )
                    if not _is_captcha_page(html):
                        break
                else:
                    # Timed out.
                    continue

            soup = BeautifulSoup(html, "lxml")

            website = _extract_company_website(soup)
            team = _extract_team_members_from_company_profile(soup)

            company_on_page = _extract_first_text(
                soup,
                [
                    "h1",
                    "[data-testid='company-name']",
                ],
            )

            return {
                "company_profile_url": url,
                "company": company_on_page,
                "company_website": website,
                "team_members": team,
            }

        return None
    finally:
        try:
            await page.close()
        except Exception:
            pass


def _extract_skills(soup: BeautifulSoup, full_text: str) -> list[str]:
    skills: list[str] = []

    # Try common headings.
    for heading in soup.find_all(["h2", "h3"]):
        title = (heading.get_text(" ", strip=True) or "").lower()
        if title in {"skills", "skill", "requirements", "required skills"}:
            container = heading.find_next()
            if container:
                # Collect nearby tags.
                for el in container.select("a, span, li"):
                    t = _clean_text(el.get_text(" ", strip=True))
                    if 1 <= len(t) <= 40 and re.match(r"^[A-Za-z0-9+.#\-/ ]+$", t):
                        skills.append(t)
            break

    # Fallback: mine from text using common tech tokens.
    fallback_tokens = [
        "node", "nodejs", "node.js", "django", "fastapi", "mongodb", "mongo", "postgres", "postgresql",
        "aws", "docker", "kubernetes", "k8s", "react", "typescript", "python", "llm", "rag",
        "langchain", "openai", "gpt", "vector", "pinecone", "weaviate",
    ]
    lower = full_text.lower()
    for tok in fallback_tokens:
        if tok in lower:
            skills.append(tok)

    # De-dupe / normalize
    normed: list[str] = []
    seen: set[str] = set()
    for s in skills:
        k = s.strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        normed.append(s.strip())

    return normed


async def scrape_wellfound_job(
    context: BrowserContext,
    url: str,
    *,
    timeout_ms: int = 60000,
    allow_manual_captcha: bool = False,
    manual_captcha_timeout_s: int = 180,
    debug_dir: str | None = None,
) -> JobLead:
    lead = JobLead(url=url)

    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        # Some pages render after initial load.
        await page.wait_for_timeout(1500)

        # Detect bot protection early.
        html = await _safe_page_content(page)
        if _is_access_restricted_page(html):
            raise RuntimeError(
                "Wellfound returned 'Access is temporarily restricted'. "
                "No job content is available to scrape right now."
            )

        if _is_captcha_page(html):
            if allow_manual_captcha:
                print(
                    f"[CAPTCHA] Blocked by bot protection for {url}. "
                    f"Please complete the CAPTCHA in the opened browser window within {manual_captcha_timeout_s}s..."
                )
                try:
                    await page.bring_to_front()
                except Exception:
                    pass

                # Give the user time to solve the CAPTCHA in headful mode.
                # We poll until the page is no longer the CAPTCHA shell.
                for _ in range(max(1, manual_captcha_timeout_s // 2)):
                    await asyncio.sleep(2)
                    if page.is_closed():
                        raise RuntimeError(
                            "Browser window was closed while waiting for CAPTCHA. "
                            "Re-run with --headful and keep the window open until scraping finishes."
                        )
                    html = await _safe_page_content(page)
                    if _is_access_restricted_page(html):
                        raise RuntimeError(
                            "Wellfound returned 'Access is temporarily restricted' after the challenge. "
                            "No job content is available to scrape right now."
                        )
                    if not _is_captcha_page(html):
                        print(f"[CAPTCHA] Cleared for {url}; continuing scrape.")
                        break
                else:
                    raise RuntimeError(
                        "Blocked by CAPTCHA. Solve it in the opened browser window (use --headful), "
                        "or reduce scraping volume."
                    )
            else:
                raise RuntimeError(
                    "Blocked by CAPTCHA (captcha-delivery.com). Re-run with --headful and a persistent profile."
                )

        # Wait for network to settle; helps with client-rendered pages.
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

        await auto_scroll(page)

        html = await _safe_page_content(page)
        if _is_access_restricted_page(html):
            raise RuntimeError(
                "Wellfound returned 'Access is temporarily restricted'. "
                "No job content is available to scrape right now."
            )
        soup = BeautifulSoup(html, "lxml")
        full_text = _clean_text(soup.get_text(" ", strip=True))

        lead.title = (
            _extract_first_text(soup, ["[data-testid='job-title']", "h1", "header h1"])
            or _extract_meta_content(soup, property_name="og:title")
            or _extract_first_text(soup, ["title"])
        )

        # Best-effort company name extraction.
        lead.company = _extract_first_text(
            soup,
            [
                "[data-testid='company-name']",
                "a[href*='/company/']",
                "a[href*='/companies/']",
                "header a",
            ],
        )

        # Description: take the biggest text block-like container when available.
        lead.description = _extract_first_text(
            soup,
            [
                "[data-testid='job-description']",
                "main",
                "article",
                "section",
            ],
        )

        lead.company_website = _extract_company_website(soup)
        
        # If no website found on job page, try fetching from Wellfound company profile.
        if not lead.company_website and lead.company:
            try:
                lead.company_website = await _fetch_company_website_from_wellfound(context, lead.company, timeout_ms)
            except Exception:
                pass
        
        lead.apply_url = _extract_apply_url(soup)
        lead.contact_url = _extract_contact_url(soup)
        lead.google_form_url = _extract_google_form_url(soup)
        lead.compensation_text = _extract_compensation(full_text)
        lead.contact_name = _extract_contact_name(full_text)
        lead.required_skills = _extract_skills(soup, full_text)
        
        # Extract team members from job posting page (e.g., "Your team", "Meet the team" sections)
        job_page_team = _extract_team_members_from_job_posting(soup)
        if job_page_team:
            lead.team_members = job_page_team
            if not lead.contact_name:
                lead.contact_name = job_page_team[0]

        if debug_dir and not (lead.title or lead.company or lead.description):
            dbg = Path(debug_dir)
            dbg.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^a-zA-Z0-9]+", "_", url).strip("_")[:120]
            html_path = dbg / f"{safe}.html"
            shot_path = dbg / f"{safe}.png"
            meta_path = dbg / f"{safe}.txt"
            html_path.write_text(html, encoding="utf-8")
            try:
                await page.screenshot(path=str(shot_path), full_page=True)
            except Exception:
                pass
            try:
                meta_path.write_text(
                    f"url={url}\nfinal_url={page.url}\npage_title={await page.title()}\nhtml_len={len(html)}\n",
                    encoding="utf-8",
                )
            except Exception:
                pass

        lead.raw = {
            "title": lead.title,
            "company": lead.company,
            "company_website": lead.company_website,
            "compensation_text": lead.compensation_text,
            "contact_name": lead.contact_name,
            "required_skills": lead.required_skills,
        }

        return lead
    finally:
        await page.close()


def lead_to_dict(lead: JobLead) -> dict:
    d = asdict(lead)
    # keep CSV-friendly
    d["required_skills"] = ", ".join(lead.required_skills)
    d["matched_skills"] = ", ".join(lead.matched_skills)
    d["inferred_emails"] = ", ".join(lead.inferred_emails)
    d["team_members"] = ", ".join(lead.team_members)
    for k in ("description", "draft_email", "error"):
        v = d.get(k)
        if isinstance(v, str) and ("\n" in v or "\r" in v):
            d[k] = v.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    try:
        d["raw"] = json.dumps(lead.raw, ensure_ascii=False)
    except Exception:
        d["raw"] = str(lead.raw)
    return d
