from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QueryConfig:
    skills: list[str]
    posted: str = "today"  # today|past_24h|week etc (string used in query)
    min_comp_usd: int = 20000
    comp_period: str = "month"  # month|year
    company_stage: str = "series_b"  # pre_revenue|seed|series_a|series_b


_STAGE_TERMS = {
    "pre_revenue": ['"pre-revenue"', '"pre revenue"', '"pre-seed"', 'preseed', '"early stage"'],
    "seed": ['seed', '"seed stage"', '"early stage"'],
    "series_a": ['"Series A"', 'series a', 'seed', '"early stage"'],
    "series_b": ['"Series B"', 'series b', '"Series A"', 'series a', 'seed', '"early stage"'],
}


def _normalize_skill(s: str) -> str:
    return s.strip().lower()


def build_wellfound_google_query(cfg: QueryConfig) -> str:
    skills = sorted({_normalize_skill(s) for s in cfg.skills if s.strip()})
    skill_terms = []
    for s in skills:
        if s in {"node", "nodejs", "node.js"}:
            skill_terms.append('("nodejs" OR "node.js" OR "node js")')
        elif s in {"kubernetes", "k8s"}:
            skill_terms.append('("kubernetes" OR k8s)')
        elif s in {"genai", "gen ai", "generative ai"}:
            skill_terms.append('("generative ai" OR genai OR "gen ai")')
        elif s in {"llm", "llms"}:
            skill_terms.append('(llm OR llms OR "large language model")')
        elif " " in s:
            skill_terms.append(f'"{s}"')
        else:
            skill_terms.append(s)

    posted_terms = []
    if cfg.posted == "today":
        posted_terms = ['"today"', '"posted today"', '"just posted"']
    elif cfg.posted == "past_24h":
        posted_terms = ['"past 24 hours"', '"last 24 hours"', '"just posted"']
    else:
        posted_terms = [f'"{cfg.posted}"']

    # Salary terms in Google queries are fuzzy; this is only for narrowing results.
    k = cfg.min_comp_usd // 1000
    if cfg.comp_period == "year":
        comp_terms = [f'"${k}k"', f'"{k}k"', '"per year"', '"annual"']
    else:
        comp_terms = [f'"${k}k"', f'"{k}k"', '"per month"', '"monthly"']

    stage_terms = _STAGE_TERMS.get(cfg.company_stage, _STAGE_TERMS["series_b"])

    # Wellfound job URLs often contain /jobs; keep it broad but biased.
    parts = [
        "site:wellfound.com",
        "(inurl:jobs OR inurl:job)",
        f"({' OR '.join(skill_terms)})" if skill_terms else "",
        f"({' OR '.join(comp_terms)})",
        f"({' OR '.join(posted_terms)})",
        f"({' OR '.join(stage_terms)})",
    ]
    return " ".join(p for p in parts if p)
