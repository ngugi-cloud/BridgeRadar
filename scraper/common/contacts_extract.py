"""Pure extraction helpers for Engine 3 (no network). Heuristics, so every result
carries its source URL and must be verified by a human before outreach."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .entities import domain_of

EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
PHONE_RX = re.compile(r"(?:\+?254|\b0)[\s\-().]*\d[\d\s\-().]{7,14}\d")
NAME_RX = re.compile(r"^(?:(?:Dr|Mr|Mrs|Ms|Eng|Prof|CPA|Hon)\.?\s+)?[A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-\.]+){1,3}$")
_BLOCK_TAGS = ["div", "li", "article", "section", "tr", "p", "td"]
_ROLE_ORDER = ["hr", "finance", "operations", "legal_compliance", "executive"]


class Rules:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.titles = {k: re.compile(v, re.I) for k, v in cfg["title_patterns"].items()}
        self.hr_locals = {x.replace(".", "") for x in cfg["role_inbox_locals"]["hr"]}
        self.generic_locals = {x.replace(".", "") for x in cfg["role_inbox_locals"]["generic"]}
        self.blocked = {d.lower() for d in cfg["blocked_email_domains"]}
        self.stop = {w.lower() for w in cfg["name_stopwords"]}
        self.keywords = [k.lower() for k in cfg["page_keywords"]]
        self.priority = [k.lower() for k in cfg["page_priority"]]


def classify_title(line: str, rules: Rules) -> str | None:
    line = re.sub(r"\s+", " ", line or "").strip()
    if not line or len(line) > 90:
        return None
    for cat in _ROLE_ORDER:
        if rules.titles[cat].search(line):
            return cat
    return None


def is_name(line: str, rules: Rules) -> bool:
    line = line.strip()
    if not (5 <= len(line) <= 45) or re.search(r"[\d@:/]", line) or not NAME_RX.match(line):
        return False
    words = re.sub(r"[^A-Za-z' ]", " ", line.lower()).split()
    return not any(w in rules.stop for w in words) and classify_title(line, rules) is None


# ------------------------------------------------------------------ pages
def pick_pages(html: str, base_url: str, rules: Rules, limit: int) -> list[str]:
    """Same-domain links that look like contact / team / leadership / careers pages."""
    base = domain_of(base_url)
    scored, seen = [], {base_url.split("#")[0].rstrip("/")}
    for i, a in enumerate(BeautifulSoup(html or "", "html.parser").find_all("a", href=True)):
        href = urljoin(base_url, a["href"]).split("#")[0]
        if domain_of(href) != base or href.rstrip("/") in seen or not href.startswith("http"):
            continue
        blob = (urlparse(href).path + " " + a.get_text(" ")).lower()
        hits = [rules.priority.index(k) for k in rules.keywords if k in blob and k in rules.priority]
        if hits:
            seen.add(href.rstrip("/"))
            scored.append((min(hits), i, href))
    return [h for _, _, h in sorted(scored)][:limit]


# ----------------------------------------------------------------- emails
def _domain_ok(email_domain: str, site_domain: str) -> bool:
    e, s = email_domain.lower(), site_domain.lower()
    return e == s or e.endswith("." + s) or s.endswith("." + e)


def extract_emails(text: str, site_domain: str, rules: Rules) -> list[str]:
    out = []
    for m in EMAIL_RX.findall(text or ""):
        m = m.lower().strip(".")
        dom = m.split("@", 1)[1]
        if dom in rules.blocked or not _domain_ok(dom, site_domain):
            continue
        if re.search(r"\.(png|jpe?g|gif|svg|webp)$", m):
            continue
        if m not in out:
            out.append(m)
    return out


def inbox_kind(email: str, rules: Rules) -> str | None:
    """'hr' | 'generic' | None (None = person-style address)."""
    local = email.split("@", 1)[0].replace(".", "").replace("-", "").replace("_", "")
    if local in rules.hr_locals:
        return "hr"
    if local in rules.generic_locals:
        return "generic"
    return None


# ----------------------------------------------------------------- phones
def extract_landlines(text: str, max_n: int = 2) -> list[str]:
    """Kenyan landlines only (area codes 02x-06x). Mobiles (07x/01x) are dropped."""
    out = []
    for m in PHONE_RX.findall(text or ""):
        s = re.sub(r"\(0\)", "", m)
        digits = re.sub(r"\D", "", s)
        if digits.startswith("254"):
            digits = digits[3:]
        elif digits.startswith("0"):
            digits = digits[1:]
        if re.fullmatch(r"[2-6]\d{7,8}", digits):
            num = "+254" + digits
            if num not in out:
                out.append(num)
    return out[:max_n]


# ----------------------------------------------------------------- people
def _page_text(soup) -> str:
    return soup.get_text("\n", strip=True)


def _split_line(line: str):
    parts = re.split(r"\s+[–—\-|:]\s+|\s*[,|]\s+", line)
    return [p.strip() for p in parts if p.strip()]


def _person_from_lines(lines: list[str], rules: Rules):
    titles = [(l, classify_title(l, rules)) for l in lines]
    titles = [(l, c) for l, c in titles if c]
    names = [l for l in lines if is_name(l, rules)]
    if len(titles) == 1 and len(names) == 1:
        return names[0], titles[0][0], titles[0][1]
    for line in lines:                                   # "Jane Wanjiru, Head of HR"
        parts = _split_line(line)
        if len(parts) >= 2:
            n = [p for p in parts if is_name(p, rules)]
            t = [(p, classify_title(p, rules)) for p in parts if classify_title(p, rules)]
            if len(n) == 1 and len(t) == 1:
                return n[0], t[0][0], t[0][1]
    return None


def extract_people(html: str, site_domain: str, rules: Rules) -> list[dict]:
    """Named people with an HR / finance / operations / legal / executive title.

    Uses the smallest page block containing a title line; skips blocks with more
    than one person (ambiguous). Emails count only if inside that same block.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    els = soup.find_all(_BLOCK_TAGS)[:3000]
    hit = {}
    for el in els:
        txt = el.get_text("\n", strip=True)
        if txt and len(txt) <= 400 and any(classify_title(p, rules) for l in txt.split("\n") for p in [l] + _split_line(l)):
            hit[id(el)] = el
    out, seen = [], set()
    for el in hit.values():
        if any(id(d) in hit for d in el.find_all(_BLOCK_TAGS)):
            continue                                      # keep only the smallest blocks
        scope = el
        person = _person_from_lines(el.get_text("\n", strip=True).split("\n"), rules)
        if not person and el.parent is not None:
            ptxt = el.parent.get_text("\n", strip=True)
            if len(ptxt) <= 500:
                person, scope = _person_from_lines(ptxt.split("\n"), rules), el.parent
        if not person:
            continue
        name, title, cat = person
        html_scope = str(scope)
        emails = [e for e in extract_emails(html_scope, site_domain, rules) if inbox_kind(e, rules) is None]
        key = (name.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "title": title, "role_category": cat,
                    "email": emails[0] if emails else ""})
    return out
