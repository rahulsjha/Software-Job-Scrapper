from __future__ import annotations

import re


def _norm(s: str) -> str:
    s = s.strip().lower()
    s = s.replace("node.js", "nodejs").replace("node js", "nodejs")
    s = s.replace("postgresql", "postgres")
    # Replace only the standalone token 'postgre' (not the substring inside 'postgres').
    s = re.sub(r"\bpostgre\b", "postgres", s)
    s = s.replace("k8s", "kubernetes")
    s = re.sub(r"\s+", " ", s)
    return s


def score_skill_match(target_skills: list[str], required_skills: list[str]) -> tuple[float, list[str]]:
    target = {_norm(s) for s in target_skills if s.strip()}
    required = {_norm(s) for s in required_skills if s.strip()}

    if not target:
        return 0.0, []

    matched = sorted({t for t in target if t in required})
    score = len(matched) / len(target)
    return score, matched
