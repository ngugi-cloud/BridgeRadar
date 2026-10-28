"""Keyword classifier driven by config/sector_map.yaml.

It is a heuristic and says so: every sector result carries a confidence level,
and a tie or no-match returns sector "other" with confidence "low" (the caller
sends low-confidence rows to needs_review). Manual overrides always win.
"""
from __future__ import annotations

import re

from .textutil import normalize_company

_ROLE_ORDER = ["hr", "clinical", "engineering", "sales_marketing",
               "finance_admin", "operations", "executive"]
_SENIORITY_ORDER = ["executive", "junior", "senior", "mid"]


def _compile(keywords):
    out = []
    for kw in keywords or []:
        kw = str(kw).strip()
        if kw:
            out.append(re.compile(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])"))
    return out


class Classifier:
    def __init__(self, sector_map: dict):
        self.map = sector_map
        self.sectors = {
            name: {"vertical": spec.get("vertical", "Other"), "pats": _compile(spec.get("keywords"))}
            for name, spec in (sector_map.get("sectors") or {}).items()
        }
        self.roles = {k: _compile(v) for k, v in (sector_map.get("role_families") or {}).items()}
        self.seniority_pats = {k: _compile(v) for k, v in (sector_map.get("seniority") or {}).items()}
        self.urgency = _compile(sector_map.get("urgency_keywords"))
        self.overrides = {normalize_company(o["company"]): o
                          for o in (sector_map.get("overrides") or []) if o.get("company")}
        v = sector_map.get("verticals") or {}
        self.target = set(v.get("target", []))
        self.adjacent = set(v.get("adjacent", []))

    def sector(self, title: str, company: str, extra: str = "") -> tuple[str, str, str]:
        """Return (sector, vertical, confidence). Company hits count double."""
        ov = self.overrides.get(normalize_company(company))
        if ov and ov.get("sector") in self.sectors:
            s = ov["sector"]
            return s, self.sectors[s]["vertical"], "high"
        t, c, e = (str(x or "").lower() for x in (title, company, extra))
        scores = {}
        for name, spec in self.sectors.items():
            n = sum(len(p.findall(t)) for p in spec["pats"])
            n += 2 * sum(len(p.findall(c)) for p in spec["pats"])
            n += sum(len(p.findall(e)) for p in spec["pats"])
            if n:
                scores[name] = n
        if not scores:
            return "other", "Other", "low"
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            return "other", "Other", "low"
        best, n = ranked[0]
        return best, self.sectors[best]["vertical"], ("high" if n >= 3 else "medium")

    def seniority(self, title: str) -> str:
        t = str(title or "").lower()
        for level in _SENIORITY_ORDER:
            if any(p.search(t) for p in self.seniority_pats.get(level, [])):
                return level
        return "unspecified"

    def role_family(self, title: str) -> str:
        t = str(title or "").lower()
        for fam in _ROLE_ORDER:
            if any(p.search(t) for p in self.roles.get(fam, [])):
                return fam
        return "other"

    def is_urgent(self, *texts: str) -> bool:
        blob = " ".join(str(x or "").lower() for x in texts)
        return any(p.search(blob) for p in self.urgency)
