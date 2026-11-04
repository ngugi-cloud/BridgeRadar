"""Normalisation helpers shared by all engines (company names, titles, dates, IDs)."""
from __future__ import annotations

import datetime as dt
import hashlib
import re

from dateutil import parser as dateparser

_SUFFIXES = {"ltd", "limited", "plc", "inc", "llc", "co", "company", "corp", "corporation"}
_FOREIGN = re.compile(
    r"\b(uganda|tanzania|rwanda|burundi|ethiopia|somalia|south sudan|nigeria|ghana|"
    r"south africa|egypt|zambia|zimbabwe|malawi|mozambique|senegal|cameroon|dubai|uae|"
    r"united arab emirates|saudi|qatar|kuwait|united kingdom|england|usa|united states|"
    r"canada|india|germany|netherlands|france|australia|singapore|belgium|spain|italy|"
    r"portugal|poland|sweden|norway|denmark|switzerland|austria|ireland|japan|china|"
    r"brazil|mexico|berlin|hamburg|munich|muenchen|frankfurt|dusseldorf|düsseldorf|"
    r"cologne|freiburg|vienna|amsterdam|paris|london|madrid|lisbon|stockholm|copenhagen)\b",
    re.I,
)
_CONFIDENTIAL = re.compile(
    r"^(confidential|anonymous|our client|a leading|a reputable|a well[- ]established|"
    r"private employer|undisclosed)\b",
    re.I,
)
_AGENCY_HINTS = re.compile(
    r"\b(recruitment|staffing|manpower|headhunt\w*|executive search|placement|"
    r"employment agency|hr consult\w*)\b",
    re.I,
)


def clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def normalize_company(name: str) -> str:
    s = clean(name).lower().replace("&", " and ")
    s = re.sub(r"\((k|kenya|ea|east africa)\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = s.split()
    while toks and toks[-1] in _SUFFIXES:
        toks.pop()
    return " ".join(toks)


def normalize_title(title: str) -> str:
    s = clean(title).lower()
    s = re.sub(r"[^a-z0-9+#/ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_location(loc: str) -> str:
    s = clean(loc).lower()
    s = re.sub(r"\((hybrid|remote|onsite|on-site)\)", " ", s)
    s = s.replace("kenya", " ")
    parts = [p.strip() for p in s.split(",")]
    parts = [re.sub(r"\bcounty\b", "", p).strip() for p in parts if p.strip()]
    parts = [p for p in parts if p]
    return parts[0] if parts else ""


def make_job_id(company: str, title: str, location: str) -> str:
    key = "|".join([normalize_company(company), normalize_title(title),
                    normalize_location(location)])
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def parse_date(value, today: dt.date | None = None) -> dt.date | None:
    """Parse a date or return None. Never invents one."""
    today = today or dt.date.today()
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            ts = value / 1000 if value > 1e11 else value
            d = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date()
        else:
            text = str(value).strip()
            # ISO dates first: dateutil's dayfirst=True would misread 2026-12-01 as 12 Jan.
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
            if m:
                d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            else:
                d = dateparser.parse(text, dayfirst=True).date()
    except (ValueError, OverflowError, OSError, TypeError):
        return None
    if d.year < 2000 or d > today + dt.timedelta(days=730):
        return None
    return d


def out_of_scope(location: str) -> bool:
    """Kenya-only scope: drop postings whose location names another country."""
    loc = clean(location)
    if not loc or re.search(r"\bkenya\b", loc, re.I):
        return False
    return bool(_FOREIGN.search(loc))


def is_confidential(company: str) -> bool:
    c = clean(company)
    return not c or bool(_CONFIDENTIAL.match(c))


def looks_like_agency(company: str) -> bool:
    return bool(_AGENCY_HINTS.search(clean(company)))
